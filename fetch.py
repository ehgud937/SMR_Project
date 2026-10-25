"""문서 수집: ADAMS 조회, 다운로드, 로컬 파일 등록. 결과는 data/docs/<문서>/ 에 쌓인다."""
import argparse
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

import httpx
import pymupdf
from dotenv import load_dotenv

ROOT = Path(__file__).parent
DOCS = ROOT / "data" / "docs"
load_dotenv(ROOT / ".env")

AGENT = "SMR-figure-research"
MIN_INTERVAL = 3.0  # 같은 호스트 요청 간격(초)
RETRY_STATUS = {429, 500, 502, 503, 504}
ADAMS_API = "https://adams-api.nrc.gov/aps/api/search"

# 도켓 → 노형. 여기 없는 도켓은 약칭을 자동으로 만들지 않는다.
DOCKET_REACTOR = {
    "05200048": "nuscale-us600",
    "05200050": "nuscale-us460",
    "99902071": "xe100",
    "99902117": "xe100",  # Long Mott
}
DOCTYPE_ABBR = [
    ("Final Safety Analysis Report", "fsar"),
    ("Preliminary Safety Analysis Report", "psar"),
    ("Topical Report", "tr"),
    ("Environmental Report", "er"),
]

_last_hit: dict[str, float] = {}
_robots: dict[str, RobotFileParser] = {}


class Blocked(Exception):
    pass


def client(**kw):
    contact = os.environ.get("SMR_CONTACT_EMAIL")
    if not contact:
        sys.exit(".env 에 SMR_CONTACT_EMAIL 을 넣어야 함 (User-Agent 연락처)")
    ua = f"{AGENT}/0.1 (internal research use of public licensing documents; contact: {contact})"
    return httpx.Client(headers={"User-Agent": ua}, timeout=120, follow_redirects=True, **kw)


def polite_get(c, url, headers=None):
    host = urlsplit(url).netloc
    for attempt in range(4):
        wait = MIN_INTERVAL - (time.monotonic() - _last_hit.get(host, -1e9))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.monotonic()
        try:
            r = c.get(url, headers=headers)
            if r.status_code not in RETRY_STATUS:
                return r
            err = f"HTTP {r.status_code}"
        except httpx.TransportError as e:
            err = e
        if attempt < 3:
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"{url}: 3회 재시도 실패 ({err})")


def parse_robots(status, text):
    """RobotFileParser.read() 와 같은 규칙: 401·403 은 전부 금지, 그 밖의 4xx 는 전부 허용."""
    rp = RobotFileParser()
    if status in (401, 403):
        rp.disallow_all = True
    elif status >= 400:
        rp.allow_all = True
    else:
        rp.parse(text.splitlines())
    return rp


def robots_ok(c, url):
    s = urlsplit(url)
    if s.netloc not in _robots:
        r = polite_get(c, f"{s.scheme}://{s.netloc}/robots.txt")
        _robots[s.netloc] = parse_robots(r.status_code, r.text)
    return _robots[s.netloc].can_fetch(AGENT, url)


def resolve(c, ml):
    """ADAMS 문서번호 조회. (메타데이터, 응답 원문)"""
    r = polite_get(c, f"{ADAMS_API}/{ml}", headers={
        "Ocp-Apim-Subscription-Key": os.environ["ADAMS_API_KEY"], "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"ADAMS {ml}: HTTP {r.status_code}")
    j = r.json()
    return j.get("document", j), r.content


def slug_from_adams(doc):
    """예: nuscale-us460-fsar-ch1-r2. 못 만들면 (None, 이유)."""
    title = doc.get("DocumentTitle") or ""
    reactors = {DOCKET_REACTOR.get(d) for d in doc.get("DocketNumber") or []} - {None}
    if len(reactors) != 1:
        return None, f"도켓 {doc.get('DocketNumber')}의 노형을 모름"
    types = " ".join(doc.get("DocumentType") or [])
    dtype = next((a for k, a in DOCTYPE_ABBR if k in types), None)
    if not dtype:
        return None, f"문서 유형 {doc.get('DocumentType')}의 약칭이 없음"
    parts = [reactors.pop(), dtype]
    if m := re.search(r"Chapter\s+(\d+)", title):
        parts.append(f"ch{m.group(1)}")
    if m := re.search(r"Revision\s+(\w+)", title):
        parts.append(f"r{m.group(1).lower()}")
    return "-".join(parts), None


def doc_dir(name, meta=None):
    d = DOCS / name
    if meta and (d / "doc.json").exists():
        old = json.loads((d / "doc.json").read_text()).get("adams_no")
        if old != meta.get("adams_no"):
            raise ValueError(f"{name} 에는 이미 다른 문서({old})가 있음. 다른 약칭을 지정해야 함")
    d.mkdir(parents=True, exist_ok=True)
    if meta:
        (d / "doc.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return d


def store(data, acq, d):
    """원본은 <sha256>.pdf 로 한 번만 쓰고(읽기 전용), 출처는 <sha256>.json 에 누적한다."""
    if not data.startswith(b"%PDF"):
        raise ValueError("PDF가 아님")
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        pages = pdf.page_count
    sha = hashlib.sha256(data).hexdigest()
    if other := [p.name for p in d.glob("*.pdf") if p.stem != sha]:
        raise ValueError(f"{d.name}에 다른 PDF가 이미 있음: {other[0]}")
    path = d / f"{sha}.pdf"
    if not path.exists():
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.rename(path)
        path.chmod(0o444)
    pj = path.with_suffix(".json")
    prov = json.loads(pj.read_text()) if pj.exists() else {"sha256": sha, "page_count": pages, "acquisitions": []}
    prov["acquisitions"].append(acq)
    pj.write_text(json.dumps(prov, ensure_ascii=False, indent=2))
    return path, prov


def now():
    return datetime.now(timezone.utc).isoformat()


def download(c, url, d):
    if not robots_ok(c, url):
        raise Blocked(f"robots.txt 금지: {url}")
    r = polite_get(c, url)
    if r.status_code != 200:
        raise Blocked(f"HTTP {r.status_code}: {url}")
    if not r.content.startswith(b"%PDF"):
        raise Blocked(f"PDF가 아닌 응답({r.headers.get('content-type')}): {url}")
    return store(r.content, {"url": url, "final_url": str(r.url), "fetched_at": now(), "acquired_via": "download",
                             "http_status": r.status_code, "response_headers": dict(r.headers)}, d)


def import_local(path, url, d):
    return store(Path(path).read_bytes(), {"url": url, "fetched_at": now(), "acquired_via": "import-local",
                                           "inbox_file": Path(path).name}, d)


def main():
    ap = argparse.ArgumentParser(description="SMR 인허가 문서 수집")
    ap.add_argument("name", nargs="?", help="문서 약칭(폴더 이름). --ml 이면 생략 가능")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ml", help="ADAMS 문서번호 (예: ML25099A239)")
    src.add_argument("--url", help="PDF 주소")
    src.add_argument("--import", dest="local", nargs=2, metavar=("PDF", "URL"), help="직접 받은 파일과 원래 주소")
    a = ap.parse_args()
    if not a.ml and not a.name:
        ap.error("--url, --import 는 문서 약칭이 필요함")

    try:
        with client() as c:
            if a.local:
                pdf, prov = import_local(a.local[0], a.local[1], doc_dir(a.name))
            elif a.url:
                pdf, prov = download(c, a.url, doc_dir(a.name))
            else:
                meta, raw = resolve(c, a.ml)
                print("ADAMS:", meta.get("Availability"), "|", meta.get("DocumentTitle"))
                name = a.name
                if not name:
                    name, why = slug_from_adams(meta)
                    if not name:
                        sys.exit(f"약칭 자동 생성 실패({why}). 약칭을 직접 지정: fetch.py <약칭> --ml {a.ml}")
                dockets = meta.get("DocketNumber") or []
                d = doc_dir(name, {"reactor": DOCKET_REACTOR.get(dockets[0]) if dockets else None,
                                   "title": meta.get("DocumentTitle"), "adams_no": a.ml,
                                   "docket": dockets, "package": meta.get("PackageNumber")})
                (d / "adams.json").write_bytes(raw)
                (d / "adams.txt").write_text(meta.get("content") or "", encoding="utf-8")
                pdf, prov = download(c, meta["Url"], d)
    except Blocked as e:
        sys.exit(f"차단됨: {e}\n직접 받아 data/inbox/ 에 넣고 --import 로 등록하면 됨")
    except ValueError as e:
        sys.exit(str(e))
    print(pdf.relative_to(ROOT), prov["page_count"], "쪽")


if __name__ == "__main__":
    main()
