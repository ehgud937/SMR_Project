"""스모크 테스트용: ADAMS 조회 → 다운로드(또는 inbox) → data/docs/{문서}/ 저장."""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import pymupdf as fitz
import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = DATA / "docs"
UA = "SMR-figure-research/0.1 (internal research collection of public licensing documents; contact: dh.kim@pia.space)"
MIN_INTERVAL = 3.0
RETRY_STATUS = {429, 500, 502, 503, 504}

_last_hit: dict[str, float] = {}
_robots: dict[str, RobotFileParser] = {}


class Blocked(Exception):
    pass


def _wait(host):
    dt = time.monotonic() - _last_hit.get(host, -1e9)
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last_hit[host] = time.monotonic()


def polite_get(client, url, headers=None):
    host = urlsplit(url).netloc
    for attempt in range(4):  # 최초 1회 + 재시도 3회
        _wait(host)
        try:
            r = client.get(url, headers=headers)
        except httpx.TransportError as e:
            err = e
        else:
            if r.status_code not in RETRY_STATUS:
                return r
            err = f"HTTP {r.status_code}"
        if attempt < 3:
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"{url}: 재시도 3회 실패 ({err})")


def robots_ok(client, url):
    s = urlsplit(url)
    if s.netloc not in _robots:
        rp = RobotFileParser()
        r = polite_get(client, f"{s.scheme}://{s.netloc}/robots.txt")
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        _robots[s.netloc] = rp
    return _robots[s.netloc].can_fetch(UA, url)


def resolve(client, ml):
    """ADAMS 조회 → (문서 dict, 응답 원문 bytes). 저장은 문서 폴더가 정해진 뒤 save_adams 로."""
    load_dotenv(ROOT / ".env")
    key = os.environ["ADAMS_API_KEY"]
    r = polite_get(client, f"https://adams-api.nrc.gov/aps/api/search/{ml}",
                   headers={"Ocp-Apim-Subscription-Key": key, "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"ADAMS {ml}: HTTP {r.status_code}")
    doc = r.json()
    return doc.get("document", doc), r.content


def save_adams(doc_dir, doc, raw):
    (doc_dir / "adams.json").write_bytes(raw)
    (doc_dir / "adams.txt").write_text(doc.get("content") or "", encoding="utf-8")


# 도켓 번호 → 노형. 모르는 도켓이면 약칭을 자동으로 만들지 않고 멈춘다.
DOCKET_REACTOR = {"05200048": "nuscale-us600", "05200050": "nuscale-us460",
                  "99902071": "xe100", "99902117": "xe100"}  # 99902117 = Long Mott (Xe-100 부지)
DOCTYPE_ABBR = [("Final Safety Analysis Report", "fsar"), ("Preliminary Safety Analysis Report", "psar"),
                ("Topical Report", "tr"), ("Environmental Report", "er")]


def slug_from_adams(doc):
    """ADAMS 메타 → 문서 약칭 (예: nuscale-us460-fsar-ch1-r2). 못 만들면 (None, 이유)."""
    title = doc.get("DocumentTitle") or ""
    reactors = {DOCKET_REACTOR.get(d) for d in doc.get("DocketNumber") or []} - {None}
    if len(reactors) != 1:
        return None, f"도켓 {doc.get('DocketNumber')} 의 노형을 모름"
    types = " ".join(doc.get("DocumentType") or [])
    dtype = next((a for k, a in DOCTYPE_ABBR if k in types), None)
    if not dtype:
        return None, f"문서 유형 {doc.get('DocumentType')} 의 약칭이 없음"
    parts = [reactors.pop(), dtype]
    if m := re.search(r"Chapter\s+(\d+)", title):
        parts.append(f"ch{m.group(1)}")
    if m := re.search(r"Revision\s+(\w+)", title):
        parts.append(f"r{m.group(1).lower()}")
    return "-".join(parts), None


def verify_pdf(data):
    if not data.startswith(b"%PDF"):
        raise ValueError("PDF 시그니처 없음")
    with fitz.open(stream=data, filetype="pdf") as d:
        return d.page_count


def store(data, acq, doc_dir):
    """원본은 {sha256}.pdf 로 한 번만 쓰고, 출처는 {sha256}.json 의 acquisitions 에 쌓는다."""
    sha = hashlib.sha256(data).hexdigest()
    pages = verify_pdf(data)
    others = [p for p in doc_dir.glob("*.pdf") if p.stem != sha]
    if others:
        raise ValueError(f"{doc_dir.name} 에 내용이 다른 PDF 가 이미 있음: {others[0].name}")
    pdf = doc_dir / f"{sha}.pdf"
    if not pdf.exists():
        tmp = pdf.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.rename(pdf)
        pdf.chmod(0o444)
    pj = pdf.with_suffix(".json")
    prov = json.loads(pj.read_text()) if pj.exists() else {"sha256": sha, "page_count": pages, "acquisitions": []}
    prov["acquisitions"].append(acq)
    pj.write_text(json.dumps(prov, ensure_ascii=False, indent=2))
    return pdf, prov


def download(client, url, doc_dir):
    if not robots_ok(client, url):
        raise Blocked(f"robots.txt 가 금지: {url}")
    r = polite_get(client, url)
    if r.status_code != 200:
        raise Blocked(f"HTTP {r.status_code}: {url}")
    acq = {"url": url, "final_url": str(r.url), "fetched_at": datetime.now(timezone.utc).isoformat(),
           "acquired_via": "download", "http_status": r.status_code, "response_headers": dict(r.headers)}
    return store(r.content, acq, doc_dir)


def import_local(path, url, doc_dir):
    acq = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(), "acquired_via": "import-local",
           "inbox_file": Path(path).name}
    return store(Path(path).read_bytes(), acq, doc_dir)


def client():
    return httpx.Client(headers={"User-Agent": UA}, timeout=120, follow_redirects=True)


def doc_dir(name, meta=None):
    """data/docs/{name}/ — name 은 figure_id 접두어와 같다 (예: nuscale-us600-fsar-ch1-r4)."""
    d = DOCS / name
    d.mkdir(parents=True, exist_ok=True)
    if meta:
        (d / "doc.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return d


if __name__ == "__main__":
    # python fetch.py --ml ML25099A239                 ADAMS 조회 → 약칭 자동 생성 → 다운로드
    # python fetch.py <문서> --ml ML20036D417          약칭을 직접 정할 때
    # python fetch.py <문서> --url https://...pdf       바로 다운로드 (doc.json 은 손으로)
    # python fetch.py <문서> --import data/inbox/X.pdf <원래 URL>
    args = sys.argv[1:]
    name = None if args[0].startswith("--") else args.pop(0)
    mode, arg = args[:2]
    try:
        with client() as c:
            if mode == "--import":
                pdf, prov = import_local(arg, args[2], doc_dir(name))
            elif mode == "--ml":
                adoc, raw = resolve(c, arg)
                print("ADAMS:", adoc.get("Availability"), "|", adoc.get("DocumentTitle"))
                if not name:
                    name, why = slug_from_adams(adoc)
                    if not name:
                        sys.exit(f"약칭 자동 생성 실패: {why} → python fetch.py <약칭> --ml {arg}")
                d = doc_dir(name, {"reactor": DOCKET_REACTOR.get((adoc.get("DocketNumber") or [None])[0]), "title": adoc.get("DocumentTitle"),
                                   "adams_no": arg, "docket": adoc.get("DocketNumber"),
                                   "package": adoc.get("PackageNumber")})
                save_adams(d, adoc, raw)
                pdf, prov = download(c, adoc["Url"], d)
            else:
                pdf, prov = download(c, arg, doc_dir(name))
    except Blocked as e:
        sys.exit(f"BLOCKED {e}\n→ 직접 받아 data/inbox/ 에 넣고 --import 로 이어서 진행")
    print(pdf.relative_to(ROOT), prov["page_count"], "쪽")
