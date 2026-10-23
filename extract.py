"""스모크 테스트용: 그림 목록 파싱 → 캡션 쪽 매핑 → Withheld 판정 → 크롭 → 메타데이터."""
import collections
import csv
import json
import re
import sys
from pathlib import Path

import imagehash
import pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent
DOCS = ROOT / "data" / "docs"
DPI = 200

FIGNO = r"(\d+(?:\.\d+)*-\d+[a-z]?)"
SHEET = r"(?:\s*\((\d+)\s+of\s+\d+\))?"  # 여러 장짜리 그림: "Figure 2.3-29 (1 of 8):"
LOF_ENTRY = re.compile(rf"^Figure\s+{FIGNO}{SHEET}\s*[:.]?\s*(.*)$")
LOF_TAIL = re.compile(r"^(.*?)[\s.]*\.\s*\.[\s.]*(\S+)\s*$")  # "제목 . . . . 1.2-21"
LOF_TAIL_LOOSE = re.compile(r"^(.*?)[\s.]+(\d+(?:\.\d+)*-\d+)\s*$")  # 점선이 끊긴 "제목 . 2.4-402", "제목 2.5-177"
CAPTION = re.compile(rf"^\s*Figure\s+{FIGNO}{SHEET}\s*[:.]\s*(.*)$")


def fig_key(m):
    """그림 번호 + 장 번호 → 매칭과 figure_id 에 쓰는 키 (예: 2.3-29-s1)."""
    return m.group(1) + (f"-s{m.group(2)}" if m.group(2) else "")
WITHHELD = re.compile(r"\bWithheld\b")
# 그림 자리에 그림 대신 들어간 비공개 안내문 (예: "designated as security-related … segregated from the PSAR")
WITHHELD_NOTICE = re.compile(r"\b(withheld|security[- ]related|SUNSI)\b", re.I)


# 일부 PDF(Word 변환본)는 글꼴 인코딩 탓에 공백이 \x03, 하이픈이 'Ͳ'(U+0372)로 나온다
_FIX = str.maketrans({"\x03": " ", "\u0372": "-", "\u00a0": " "})


def ptext(page):
    return page.get_text().translate(_FIX)


def norm_title(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("’", "'").replace("”", '"')).strip()


def page_lines(page):
    """(text, bbox) 줄 목록."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).translate(_FIX).strip()
            if t:
                out.append((t, pymupdf.Rect(l["bbox"])))
    return out


# ---------- 그림 목록 ----------

LOF_HEAD = re.compile(r"^\s*(MASTER\s+)?LIST OF FIGURES\s*$", re.M | re.I)
DOT_LEADER = re.compile(r"(\.\s){4,}")


def is_list_page(text):
    """점선 줄이 3줄 이상이면 목차·그림 목록·표 목록 페이지로 본다 (문서 중간의 장별 목록 포함)."""
    return sum(bool(DOT_LEADER.search(l)) for l in text.splitlines()) >= 3


def find_lof_pages(doc, max_pages=150):
    """앞부분의 그림 목록. 제목이 있는 쪽부터 그림 항목이 이어지는 쪽까지."""
    out = []
    for i in range(min(max_pages, doc.page_count)):
        t = ptext(doc[i])
        has_entries = sum(bool(LOF_ENTRY.match(l.strip())) for l in t.splitlines()) >= 2
        if LOF_HEAD.search(t) and has_entries:
            out.append(i)
        elif out and out[-1] == i - 1 and has_entries and is_list_page(t):
            out.append(i)
        elif out:
            break
    return out


def toc_pages(doc, max_pages=40):
    return [i for i in range(min(max_pages, doc.page_count))
            if re.search(r"^\s*(TABLE OF CONTENTS|LIST OF TABLES|CONTENTS)\s*$", ptext(doc[i]), re.M | re.I)]


def parse_lof_text(text):
    """그림 목록 텍스트 → [{fig_no, title, printed_page}]. 제목이 여러 줄로 이어지는 것도 처리."""
    entries, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LOF_ENTRY.match(line)
        if m:
            cur = {"key": fig_key(m), "fig_no": m.group(1), "sheet": m.group(2) and int(m.group(2)),
                   "title": m.group(3).strip(), "printed_page": None}
            entries.append(cur)
        elif cur is not None and cur["printed_page"] is None:
            cur["title"] = (cur["title"] + " " + line).strip()
        else:
            continue
        mt = LOF_TAIL.match(cur["title"])
        if not mt:
            # 점선이 없으면, 쪽 표기가 그림 번호와 같은 장(첫 숫자)으로 시작할 때만 쪽으로 본다
            mt = LOF_TAIL_LOOSE.match(cur["title"])
            if mt and mt.group(2).split(".")[0].split("-")[0] != cur["fig_no"].split(".")[0].split("-")[0]:
                mt = None
        if mt:
            cur["title"], cur["printed_page"] = mt.group(1).strip(" ."), mt.group(2)
    return entries


# ---------- 머리글/바닥글 ----------

def _edge(page, r, zone=0.15):
    """줄이 페이지 가장자리 띠 안에 있으면 그 변 이름."""
    W, H = page.rect.width, page.rect.height
    d = {"top": r.y1, "bottom": H - r.y0, "left": r.x1, "right": W - r.x0}
    side = min(d, key=d.get)
    lim = (H if side in ("top", "bottom") else W) * zone
    return side if d[side] < lim else None


def furniture_body(doc):
    """여러 쪽에서 같은 자리에 반복되는 가장자리 줄(머리글·바닥글)을 찾아,
    그걸 뺀 본문 영역을 돌려주는 함수를 만든다. 좌표는 회전 전 기준이고 네 변을 다 본다."""
    key = lambda t, r, p: (re.sub(r"\d", "#", t), _edge(p, r))  # 가로·세로 쪽이 섞여도 같은 변이면 같은 머리글
    seen = collections.Counter()
    for p in doc:
        for t, r in page_lines(p):
            if _edge(p, r):
                seen[key(t, r, p)] += 1
    rep = {k for k, v in seen.items() if v >= 3}

    def body(page):
        b = pymupdf.Rect(page.rect)
        for t, r in page_lines(page):
            side = _edge(page, r)
            if side and (key(t, r, page) in rep or PAGE_LABEL.fullmatch(t)):
                if side == "top": b.y0 = max(b.y0, r.y1 + 1)
                elif side == "bottom": b.y1 = min(b.y1, r.y0 - 1)
                elif side == "left": b.x0 = max(b.x0, r.x1 + 1)
                else: b.x1 = min(b.x1, r.x0 - 1)
        return b
    return body


PAGE_LABEL = re.compile(r"(\d+(\.\d+)*-\d+|[ivxlc]+)|(\d+) of \d+")


def printed_label(page):
    """가장자리 띠 안의 '1.2-24' 같은 쪽 표기."""
    for t, r in page_lines(page):
        m = _edge(page, r) and PAGE_LABEL.fullmatch(t)
        if m:
            return m.group(1) or m.group(3)
    return None


# ---------- 캡션 매핑 ----------

def find_captions(doc, skip):
    hits = collections.defaultdict(list)
    for i, p in enumerate(doc):
        if i in skip:
            continue
        for t, r in page_lines(p):
            m = CAPTION.match(t)
            if m:
                hits[fig_key(m)].append((i, m.group(3).strip(), r))
    return hits


def pick_caption(cands, entry, labels):
    def score(c):
        i, title, _ = c
        s = 0
        if entry.get("printed_page") and labels[i] == entry["printed_page"]:
            s += 2
        if entry.get("title") and norm_title(title) and norm_title(entry["title"]).startswith(norm_title(title)[:30]):
            s += 1
        return s
    return max(cands, key=score)


# ---------- 내용 영역 ----------

def content_boxes(page, body, caption_rect, skip_xrefs=frozenset()):
    """본문 영역 안의 드로잉·이미지 박스, 그리고 크롭에서 지울 줄(머리글 구분선, 여백의 개정 표시줄) 목록."""
    area = body.width * body.height
    vec, ras, bars = [], [], []
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"]) & body
        if r.is_empty or r.width * r.height > 0.97 * area:
            continue
        if min(r.width, r.height) < 2 and max(r.width, r.height) > 0.6 * max(body.width, body.height):  # 머리글/바닥글 구분선
            bars.append(r)
            continue
        W, H = page.rect.width, page.rect.height
        if min(r.width, r.height) < 4 and not (0.1 * W < (r.x0 + r.x1) / 2 < 0.9 * W
                                               and 0.1 * H < (r.y0 + r.y1) / 2 < 0.9 * H):  # 여백의 개정 표시줄
            bars.append(r)
            continue
        if caption_rect and r.intersects(caption_rect):
            continue
        vec.append(r)
    for im in page.get_image_info(xrefs=True):
        if im.get("xref") in skip_xrefs:  # 여러 쪽에 반복되는 로고
            continue
        r = pymupdf.Rect(im["bbox"]) & body
        if not r.is_empty and r.width > 5 and r.height > 5:
            ras.append(r)
    return vec, ras, bars


def union(rects):
    u = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    return u


OTHER_CAPTION = re.compile(rf"^\s*(Figure|Table)\s+\S+{SHEET}\s*[:.]", re.I)


def _gap(a, b):
    return max(0, a.x0 - b.x1, b.x0 - a.x1, a.y0 - b.y1, b.y0 - a.y1)


def figure_block(page, body, fig_clip, caption_rect, max_gap=24, pad=8):
    """그림 영역에 캡션, 범례, 주석('Not to Scale', 'This is a representative figure…' 등)을 붙인 블록.
    본문 문단(여러 줄이면서 본문 폭의 60% 이상)과 다른 그림·표 캡션은 붙이지 않는다."""
    block = pymupdf.Rect(fig_clip)
    if caption_rect:
        block |= caption_rect
    long_side = max(body.width, body.height)
    cands = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        r = pymupdf.Rect(b["bbox"]) & body
        lines = ["".join(s["text"] for s in l["spans"]).strip() for l in b["lines"]]
        lines = [t for t in lines if t]
        if r.is_empty or not lines:
            continue
        if caption_rect and r.intersects(caption_rect):
            cands.append(r)  # 캡션이 두 줄 이상으로 이어지는 경우
            continue
        if any(OTHER_CAPTION.match(t) for t in lines):
            continue
        if len(lines) >= 3 and max(r.width, r.height) > 0.6 * long_side and not r.intersects(fig_clip):
            continue  # 본문 문단
        cands.append(r)
    grown = True
    while grown:
        grown = False
        for r in cands:
            if not r in block and _gap(r, block) <= max_gap:
                block |= r
                grown = True
    return (block + (-pad, -pad, pad, pad)) & body


def figure_band(page, body, crect, page_caps, skip_xrefs):
    """한 쪽에 캡션이 여러 개면 이 캡션이 맡은 세로 구간으로 본문을 좁힌다.
    캡션이 그림 위에 오는지 아래에 오는지는 이 쪽의 첫 캡션과 첫 내용의 위치로 정한다."""
    caps = sorted({tuple(r) for r in page_caps if r.width > r.height}, key=lambda r: r[1])
    caps = [pymupdf.Rect(r) for r in caps]
    if len(caps) < 2 or crect.width <= crect.height:
        return body
    vec, ras, _ = content_boxes(page, body, None, skip_xrefs)
    tops = [r.y0 for r in vec + ras if not any(r.intersects(c) for c in caps)]
    if not tops:
        return body
    k = next(j for j, c in enumerate(caps) if c.intersects(crect))
    if caps[0].y1 <= min(tops) + 5:  # 캡션이 그림 위
        y0, y1 = caps[k].y0, (caps[k + 1].y0 - 1 if k + 1 < len(caps) else body.y1)
    else:  # 캡션이 그림 아래 (두 번째 줄로 넘어간 캡션까지 30pt 여유)
        y0, y1 = (caps[k - 1].y1 + 30 if k > 0 else body.y0), caps[k].y1 + 30
    return body & pymupdf.Rect(body.x0, y0, body.x1, y1)


def judge_and_crop(page, body, caption_rect, out_png, rotation=0, skip_xrefs=frozenset()):
    text = ptext(page)
    if WITHHELD.search(text):
        return {"status": "withheld", "reason": "text:Withheld"}
    vec, ras, bars = content_boxes(page, body, caption_rect, skip_xrefs)
    body_area = body.width * body.height
    ras_area = sum(r.width * r.height for r in ras)
    vec_box = union(vec) if vec else None
    if ras_area > 0.02 * body_area:
        clip, method = union(ras), "raster"
        if vec_box and vec_box.width * vec_box.height > 0.02 * body_area:
            clip |= vec_box
            method = "raster+vector"
    elif vec_box and vec_box.width * vec_box.height > 0.02 * body_area and len(vec) >= 3:
        clip, method = vec_box, "vector"
    else:
        # 캡션 외 글자 수로 '내용 거의 없음' 판단
        body_txt = "".join(t for t, r in page_lines(page)
                           if r.intersects(body) and not (caption_rect and r.intersects(caption_rect)))
        if WITHHELD_NOTICE.search(body_txt):
            return {"status": "withheld", "reason": "notice:" + WITHHELD_NOTICE.search(body_txt).group(1).lower()}
        if len(body_txt) < 40:
            # "[[ … ]]XE" 처럼 기밀 괄호만 남은 쪽은 이유를 따로 남긴다
            return {"status": "withheld", "reason": "proprietary-brackets" if "[[" in text else "empty-body"}
        clip, method = body, "page"
    # 그림 밖에 붙은 글자(라벨, NOT TO SCALE 등)도 포함
    near = clip + (-20, -20, 20, 20)
    for t, r in page_lines(page):
        if r.intersects(near) and r.intersects(body) and not (caption_rect and r.intersects(caption_rect)):
            clip |= r & body
    fig_clip = (clip + (-6, -6, 6, 6)) & body
    clip = figure_block(page, body, fig_clip, caption_rect)
    pix = page.get_pixmap(dpi=DPI, clip=clip)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    k = DPI / 72  # 블록 안에 들어온 구분선·개정 표시줄은 크롭 이미지에서만 흰색으로 지운다
    for b in bars:
        if b.intersects(clip):
            ImageDraw.Draw(im).rectangle([(b.x0 - clip.x0 - 2) * k, (b.y0 - clip.y0 - 2) * k,
                                          (b.x1 - clip.x0 + 2) * k, (b.y1 - clip.y0 + 2) * k], fill="white")
    if rotation:  # 브라우저에서 보이는 방향으로 (PDF 회전은 시계 방향)
        im = im.rotate(-rotation, expand=True)
    im.save(out_png)
    return {"status": "public", "crop_method": method, "bbox_pt_unrotated": [round(v, 1) for v in clip],
            "figure_only_bbox_pt_unrotated": [round(v, 1) for v in fig_clip],
            "page_rotation": rotation, "width_px": im.width, "height_px": im.height, "phash": str(imagehash.phash(im))}


def run(doc_dir):
    """data/docs/{문서}/ 하나를 처리. 메타는 doc.json, 원본은 {sha256}.pdf (폴더에 하나)."""
    meta = json.loads((doc_dir / "doc.json").read_text())
    (pdf_path,) = doc_dir.glob("*.pdf")
    prov = json.loads(pdf_path.with_suffix(".json").read_text())
    fig_dir = doc_dir / "figures"
    fig_dir.mkdir(exist_ok=True)
    doc = pymupdf.open(pdf_path)
    lof_p = find_lof_pages(doc)
    skip = set(lof_p) | set(toc_pages(doc)) | {i for i in range(doc.page_count) if is_list_page(ptext(doc[i]))}
    entries = []
    for i in lof_p:
        entries += parse_lof_text(ptext(doc[i]))
    # 회전은 메모리에서만 풀고 (원본 파일은 그대로), 크롭 뒤 이미지를 다시 돌린다
    rotations = [p.rotation for p in doc]
    for p in doc:
        p.set_rotation(0)
    body_of = furniture_body(doc)
    # 여러 쪽(20% 이상)에 같은 이미지 객체로 반복되는 것은 로고로 보고 그림에서 뺀다
    xref_pages = collections.Counter(x for p in doc for x in {im.get("xref") for im in p.get_image_info(xrefs=True)})
    logos = frozenset(x for x, c in xref_pages.items() if x and c >= max(3, 0.2 * doc.page_count))
    labels = {i: printed_label(doc[i]) for i in range(doc.page_count)}
    caps = find_captions(doc, skip)
    page_caps = collections.defaultdict(list)
    for hits in caps.values():
        for i, _, r in hits:
            page_caps[i].append(r)

    rows = []
    listed = {e["key"] for e in entries}
    extra = [k for k in caps if k not in listed]
    extra_entries = [{"key": k, "fig_no": k.split("-s")[0], "sheet": int(k.split("-s")[1]) if "-s" in k else None,
                      "title": None, "printed_page": None} for k in extra]
    for e in entries + extra_entries:
        fid = f"{doc_dir.name}-fig-{e['key']}"
        row = {"figure_id": fid, "reactor": meta["reactor"], "document_title": meta["title"],
               "adams_no": meta.get("adams_no"), "source_url": prov["acquisitions"][0]["url"], "sha256": prov["sha256"],
               "fig_no": e["fig_no"], "sheet": e["sheet"], "lof_title": e["title"], "printed_page_lof": e["printed_page"],
               "in_lof": e["title"] is not None, "figure_type": None}
        cands = caps.get(e["key"])
        if not cands:
            row.update(status="unmapped", pdf_page=None)
        else:
            i, cap, crect = pick_caption(cands, e, labels)
            page = doc[i]
            body = figure_band(page, body_of(page), crect, page_caps[i], logos)
            row.update(pdf_page=i + 1, printed_page=labels[i], caption=f"Figure {e['fig_no']}" + (f" (sheet {e['sheet']})" if e["sheet"] else "") + f": {cap}",
                       other_caption_pages=[c[0] + 1 for c in cands if c[0] != i])
            res = judge_and_crop(page, body, crect, fig_dir / f"{fid}.png", rotations[i], logos)
            row.update(res)
            if res["status"] == "public":
                row["crop_path"] = str((fig_dir / f"{fid}.png").relative_to(ROOT))
        (fig_dir / f"{fid}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2))
        rows.append(row)
    return rows, {"lof_pages": [p + 1 for p in lof_p], "skip_pages": sorted(p + 1 for p in skip),
                  "lof_entries": len(entries), "extra_captions": extra}


INDEX_COLS = ["figure_id", "reactor", "document_title", "adams_no", "source_url", "sha256", "pdf_page",
              "printed_page", "printed_page_lof", "fig_no", "sheet", "caption", "status", "reason", "crop_method",
              "width_px", "height_px", "phash", "figure_type", "crop_path"]


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, INDEX_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_index():
    """문서별 figures.csv 와 전체 data/index.csv."""
    all_rows = []
    for d in sorted(DOCS.iterdir()):
        rows = [json.loads(p.read_text()) for p in sorted((d / "figures").glob("*.json"))]
        if rows:
            _write_csv(d / "figures.csv", rows)
            all_rows += rows
    _write_csv(DOCS.parent / "index.csv", all_rows)
    return len(all_rows)


if __name__ == "__main__":  # python extract.py <문서 폴더 이름> ...
    for name in sys.argv[1:]:
        rows, info = run(DOCS / name)
        print(name, info, dict(collections.Counter(r["status"] for r in rows)))
    print("index rows:", write_index())
