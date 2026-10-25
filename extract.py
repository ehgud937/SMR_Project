"""도면 추출: 그림 목록 → 캡션 쪽 매핑 → 비공개 판정 → 크롭 → 메타데이터."""
import argparse
import collections
import csv
import json
import re
from pathlib import Path

import imagehash
import pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent
DOCS = ROOT / "data" / "docs"
DPI = 200

FIGNO = r"(\d+(?:\.\d+)*-\d+[a-z]?)"
SHEET = r"(?:\s*\((\d+)\s+of\s+\d+\))?"  # "Figure 2.3-29 (1 of 8):"
LOF_HEAD = re.compile(r"^\s*(MASTER\s+)?LIST OF FIGURES\s*$", re.M | re.I)
LOF_ENTRY = re.compile(rf"^Figure\s+{FIGNO}{SHEET}\s*[:.]?\s*(.*)$")
LOF_TAIL = re.compile(r"^(.*?)[\s.]*\.\s*\.[\s.]*(\S+)\s*$")         # "제목 . . . . 1.2-21"
LOF_TAIL_LOOSE = re.compile(r"^(.*?)[\s.]+(\d+(?:\.\d+)*-\d+)\s*$")  # "제목 . 2.4-402", "제목 2.5-177"
DOT_LEADER = re.compile(r"(\.\s){4,}")
TOC_HEAD = re.compile(r"^\s*(TABLE OF CONTENTS|LIST OF TABLES|CONTENTS)\s*$", re.M | re.I)
CAPTION = re.compile(rf"^\s*Figure\s+{FIGNO}{SHEET}\s*[:.]\s*(.*)$")
OTHER_CAPTION = re.compile(rf"^\s*(Figure|Table)\s+\S+{SHEET}\s*[:.]", re.I)
PAGE_LABEL = re.compile(r"(\d+(\.\d+)*-\d+|[ivxlc]+)|(\d+) of \d+")
WITHHELD = re.compile(r"\bWithheld\b")
WITHHELD_NOTICE = re.compile(r"\b(withheld|security[- ]related|SUNSI)\b", re.I)  # 그림 대신 들어간 안내문

# Word 변환 PDF 중에 공백이 \x03, 하이픈이 U+0372 로 나오는 것이 있음
_FIX = str.maketrans({"\x03": " ", "Ͳ": "-", " ": " "})

INDEX_COLS = ["figure_id", "reactor", "document_title", "adams_no", "source_url", "sha256", "pdf_page",
              "printed_page", "printed_page_lof", "fig_no", "sheet", "caption", "status", "reason", "crop_method",
              "width_px", "height_px", "phash", "figure_type", "crop_path"]


def ptext(page):
    return page.get_text().translate(_FIX)


def page_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            t = "".join(s["text"] for s in l["spans"]).translate(_FIX).strip()
            if t:
                out.append((t, pymupdf.Rect(l["bbox"])))
    return out


def norm_title(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("’", "'").replace("”", '"')).strip()


def fig_key(m):
    """그림 번호 + 장 번호. 예: 2.3-29-s1"""
    return m.group(1) + (f"-s{m.group(2)}" if m.group(2) else "")


def union(rects):
    u = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    return u


# 그림 목록

def is_list_page(text):
    """점선 줄이 3줄 이상이면 목차류(장별 목록 포함)."""
    return sum(bool(DOT_LEADER.search(l)) for l in text.splitlines()) >= 3


def find_lof_pages(doc, max_pages=150):
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
    return [i for i in range(min(max_pages, doc.page_count)) if TOC_HEAD.search(ptext(doc[i]))]


def parse_lof_text(text):
    entries, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if m := LOF_ENTRY.match(line):
            cur = {"key": fig_key(m), "fig_no": m.group(1), "sheet": m.group(2) and int(m.group(2)),
                   "title": m.group(3).strip(), "printed_page": None}
            entries.append(cur)
        elif cur and cur["printed_page"] is None:
            cur["title"] = (cur["title"] + " " + line).strip()
        else:
            continue
        mt = LOF_TAIL.match(cur["title"])
        if not mt:
            # 점선이 없으면 그림 번호와 같은 장으로 시작하는 표기만 쪽으로 인정
            mt = LOF_TAIL_LOOSE.match(cur["title"])
            if mt and mt.group(2).split(".")[0].split("-")[0] != cur["fig_no"].split(".")[0].split("-")[0]:
                mt = None
        if mt:
            cur["title"], cur["printed_page"] = mt.group(1).strip(" ."), mt.group(2)
    return entries


# 머리글·바닥글 (좌표는 회전 전 기준, 네 변 모두)

def _edge(page, r, zone=0.15):
    W, H = page.rect.width, page.rect.height
    d = {"top": r.y1, "bottom": H - r.y0, "left": r.x1, "right": W - r.x0}
    side = min(d, key=d.get)
    return side if d[side] < (H if side in ("top", "bottom") else W) * zone else None


def furniture_body(doc):
    """3쪽 이상 같은 변에 반복되는 줄을 머리글·바닥글로 보고, 그걸 뺀 본문 영역 함수를 돌려준다."""
    key = lambda t, r, p: (re.sub(r"\d", "#", t), _edge(p, r))
    seen = collections.Counter(key(t, r, p) for p in doc for t, r in page_lines(p) if _edge(p, r))
    rep = {k for k, v in seen.items() if v >= 3}

    def body(page):
        b = pymupdf.Rect(page.rect)
        for t, r in page_lines(page):
            side = _edge(page, r)
            if not side or not (key(t, r, page) in rep or PAGE_LABEL.fullmatch(t)):
                continue
            if side == "top":
                b.y0 = max(b.y0, r.y1 + 1)
            elif side == "bottom":
                b.y1 = min(b.y1, r.y0 - 1)
            elif side == "left":
                b.x0 = max(b.x0, r.x1 + 1)
            else:
                b.x1 = min(b.x1, r.x0 - 1)
        return b
    return body


def printed_label(page):
    for t, r in page_lines(page):
        if (m := _edge(page, r) and PAGE_LABEL.fullmatch(t)):
            return m.group(1) or m.group(3)
    return None


# 캡션

def find_captions(doc, skip):
    hits = collections.defaultdict(list)
    for i, p in enumerate(doc):
        if i in skip:
            continue
        for t, r in page_lines(p):
            if m := CAPTION.match(t):
                hits[fig_key(m)].append((i, m.group(3).strip(), r))
    return hits


def pick_caption(cands, entry, labels):
    """같은 번호가 여러 쪽에 있으면 인쇄 쪽 표기 > 제목 일치 순으로 고른다."""
    def score(c):
        i, title, _ = c
        s = 2 if entry.get("printed_page") and labels[i] == entry["printed_page"] else 0
        if entry.get("title") and norm_title(title) and norm_title(entry["title"]).startswith(norm_title(title)[:30]):
            s += 1
        return s
    return max(cands, key=score)


# 그림 영역

def content_boxes(page, body, caption_rect, skip_xrefs=frozenset()):
    """(벡터 박스, 이미지 박스, 지울 줄). 지울 줄 = 머리글 구분선, 여백의 개정 표시줄."""
    area = body.width * body.height
    W, H = page.rect.width, page.rect.height
    vec, ras, bars = [], [], []
    for d in page.get_drawings():
        r = pymupdf.Rect(d["rect"]) & body
        if r.is_empty or r.width * r.height > 0.97 * area:
            continue
        thin, long = min(r.width, r.height), max(r.width, r.height)
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if thin < 2 and long > 0.6 * max(body.width, body.height):
            bars.append(r)
        elif thin < 4 and not (0.1 * W < cx < 0.9 * W and 0.1 * H < cy < 0.9 * H):
            bars.append(r)
        elif not (caption_rect and r.intersects(caption_rect)):
            vec.append(r)
    for im in page.get_image_info(xrefs=True):
        if im.get("xref") in skip_xrefs:
            continue
        r = pymupdf.Rect(im["bbox"]) & body
        if not r.is_empty and r.width > 5 and r.height > 5:
            ras.append(r)
    return vec, ras, bars


def _gap(a, b):
    return max(0, a.x0 - b.x1, b.x0 - a.x1, a.y0 - b.y1, b.y0 - a.y1)


def figure_block(page, body, fig_clip, caption_rect, max_gap=24, pad=8):
    """그림 영역에 캡션·범례·주석을 붙인다. 본문 문단과 다른 캡션은 제외."""
    block = pymupdf.Rect(fig_clip)
    if caption_rect:
        block |= caption_rect
    long_side = max(body.width, body.height)
    cands = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        r = pymupdf.Rect(b["bbox"]) & body
        lines = [t for t in ("".join(s["text"] for s in l["spans"]).strip() for l in b["lines"]) if t]
        if r.is_empty or not lines:
            continue
        if caption_rect and r.intersects(caption_rect):
            cands.append(r)
        elif any(OTHER_CAPTION.match(t) for t in lines):
            continue
        elif len(lines) >= 3 and max(r.width, r.height) > 0.6 * long_side and not r.intersects(fig_clip):
            continue  # 본문 문단
        else:
            cands.append(r)
    grown = True
    while grown:
        grown = False
        for r in cands:
            if r not in block and _gap(r, block) <= max_gap:
                block |= r
                grown = True
    return (block + (-pad, -pad, pad, pad)) & body


def figure_band(page, body, crect, page_caps, skip_xrefs):
    """한 쪽에 캡션이 여럿이면 이 캡션 몫의 세로 구간으로 본문을 좁힌다."""
    caps = [pymupdf.Rect(r) for r in sorted({tuple(r) for r in page_caps if r.width > r.height}, key=lambda r: r[1])]
    if len(caps) < 2 or crect.width <= crect.height:
        return body
    vec, ras, _ = content_boxes(page, body, None, skip_xrefs)
    tops = [r.y0 for r in vec + ras if not any(r.intersects(c) for c in caps)]
    if not tops:
        return body
    k = next(j for j, c in enumerate(caps) if c.intersects(crect))
    if caps[0].y1 <= min(tops) + 5:  # 캡션이 그림 위
        y0, y1 = caps[k].y0, (caps[k + 1].y0 - 1 if k + 1 < len(caps) else body.y1)
    else:  # 캡션이 그림 아래, 두 줄 캡션 여유 30pt
        y0, y1 = (caps[k - 1].y1 + 30 if k > 0 else body.y0), caps[k].y1 + 30
    return body & pymupdf.Rect(body.x0, y0, body.x1, y1)


def judge_and_crop(page, body, caption_rect, out_png, rotation=0, skip_xrefs=frozenset()):
    text = ptext(page)
    if WITHHELD.search(text):
        return {"status": "withheld", "reason": "text:Withheld"}
    vec, ras, bars = content_boxes(page, body, caption_rect, skip_xrefs)
    body_area = body.width * body.height
    vec_box = union(vec) if vec else None
    vec_area = vec_box.width * vec_box.height if vec_box else 0
    if sum(r.width * r.height for r in ras) > 0.02 * body_area:
        clip, method = union(ras), "raster"
        if vec_area > 0.02 * body_area:
            clip |= vec_box
            method = "raster+vector"
    elif vec_area > 0.02 * body_area and len(vec) >= 3:
        clip, method = vec_box, "vector"
    else:
        body_txt = "".join(t for t, r in page_lines(page)
                           if r.intersects(body) and not (caption_rect and r.intersects(caption_rect)))
        if m := WITHHELD_NOTICE.search(body_txt):
            return {"status": "withheld", "reason": "notice:" + m.group(1).lower()}
        if len(body_txt) < 40:
            return {"status": "withheld", "reason": "proprietary-brackets" if "[[" in text else "empty-body"}
        clip, method = body, "page"

    near = clip + (-20, -20, 20, 20)  # 그림에 붙은 라벨
    for t, r in page_lines(page):
        if r.intersects(near) and r.intersects(body) and not (caption_rect and r.intersects(caption_rect)):
            clip |= r & body
    fig_clip = (clip + (-6, -6, 6, 6)) & body
    clip = figure_block(page, body, fig_clip, caption_rect)

    pix = page.get_pixmap(dpi=DPI, clip=clip)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    k = DPI / 72
    for b in bars:  # 크롭 이미지에서만 지움
        if b.intersects(clip):
            ImageDraw.Draw(im).rectangle([(b.x0 - clip.x0 - 2) * k, (b.y0 - clip.y0 - 2) * k,
                                          (b.x1 - clip.x0 + 2) * k, (b.y1 - clip.y0 + 2) * k], fill="white")
    if rotation:  # PDF 회전은 시계 방향
        im = im.rotate(-rotation, expand=True)
    im.save(out_png)
    return {"status": "public", "crop_method": method,
            "bbox_pt_unrotated": [round(v, 1) for v in clip],
            "figure_only_bbox_pt_unrotated": [round(v, 1) for v in fig_clip],
            "page_rotation": rotation, "width_px": im.width, "height_px": im.height,
            "phash": str(imagehash.phash(im))}


def run(doc_dir):
    meta = json.loads((doc_dir / "doc.json").read_text())
    (pdf_path,) = doc_dir.glob("*.pdf")
    prov = json.loads(pdf_path.with_suffix(".json").read_text())
    fig_dir = doc_dir / "figures"
    fig_dir.mkdir(exist_ok=True)
    doc = pymupdf.open(pdf_path)

    lof_p = find_lof_pages(doc)
    skip = set(lof_p) | set(toc_pages(doc)) | {i for i in range(doc.page_count) if is_list_page(ptext(doc[i]))}
    entries = [e for i in lof_p for e in parse_lof_text(ptext(doc[i]))]

    # 회전은 메모리에서만 풀고 크롭 후 이미지를 돌린다. 원본 파일은 건드리지 않음
    rotations = [p.rotation for p in doc]
    for p in doc:
        p.set_rotation(0)
    body_of = furniture_body(doc)
    # 전체 쪽의 20% 이상에 반복되는 이미지는 로고
    xref_pages = collections.Counter(x for p in doc for x in {im.get("xref") for im in p.get_image_info(xrefs=True)})
    logos = frozenset(x for x, c in xref_pages.items() if x and c >= max(3, 0.2 * doc.page_count))
    labels = {i: printed_label(doc[i]) for i in range(doc.page_count)}
    caps = find_captions(doc, skip)
    page_caps = collections.defaultdict(list)
    for hits in caps.values():
        for i, _, r in hits:
            page_caps[i].append(r)

    n_lof = len(entries)
    listed = {e["key"] for e in entries}
    extra = [k for k in caps if k not in listed]
    entries += [{"key": k, "fig_no": k.split("-s")[0], "sheet": int(k.split("-s")[1]) if "-s" in k else None,
                 "title": None, "printed_page": None} for k in extra]
    rows = []
    for e in entries:
        fid = f"{doc_dir.name}-fig-{e['key']}"
        row = {"figure_id": fid, "reactor": meta["reactor"], "document_title": meta["title"],
               "adams_no": meta.get("adams_no"), "source_url": prov["acquisitions"][0]["url"],
               "sha256": prov["sha256"], "fig_no": e["fig_no"], "sheet": e["sheet"], "lof_title": e["title"],
               "printed_page_lof": e["printed_page"], "in_lof": e["title"] is not None, "figure_type": None}
        if not (cands := caps.get(e["key"])):
            row.update(status="unmapped", pdf_page=None)
        else:
            i, cap, crect = pick_caption(cands, e, labels)
            page = doc[i]
            sheet = f" (sheet {e['sheet']})" if e["sheet"] else ""
            row.update(pdf_page=i + 1, printed_page=labels[i], caption=f"Figure {e['fig_no']}{sheet}: {cap}",
                       other_caption_pages=[c[0] + 1 for c in cands if c[0] != i])
            body = figure_band(page, body_of(page), crect, page_caps[i], logos)
            row.update(judge_and_crop(page, body, crect, fig_dir / f"{fid}.png", rotations[i], logos))
            if row["status"] == "public":
                row["crop_path"] = str((fig_dir / f"{fid}.png").relative_to(ROOT))
        (fig_dir / f"{fid}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2))
        rows.append(row)
    return rows, {"lof_pages": [p + 1 for p in lof_p], "lof_entries": n_lof, "extra_captions": extra}


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, INDEX_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_index():
    """문서별 figures.csv 와 전체 data/index.csv"""
    all_rows = []
    for d in sorted(DOCS.iterdir()):
        if rows := [json.loads(p.read_text()) for p in sorted((d / "figures").glob("*.json"))]:
            _write_csv(d / "figures.csv", rows)
            all_rows += rows
    _write_csv(DOCS.parent / "index.csv", all_rows)
    return len(all_rows)


def main():
    ap = argparse.ArgumentParser(description="SMR 문서에서 도면 추출")
    ap.add_argument("names", nargs="*", help="data/docs 아래 문서 폴더 이름. 생략하면 전부")
    names = ap.parse_args().names or sorted(p.name for p in DOCS.iterdir() if p.is_dir())
    for name in names:
        rows, info = run(DOCS / name)
        stat = dict(collections.Counter(r["status"] for r in rows))
        print(f"{name}: 그림 목록 {info['lof_entries']}개, {stat}")
    print("index.csv:", write_index(), "행")


if __name__ == "__main__":
    main()
