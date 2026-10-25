"""그림 목록·캡션·Withheld 판정 파서 테스트. 실제 PDF 대신 텍스트 조각과 합성 PDF를 쓴다."""
import json
import sys
from pathlib import Path

import httpx
import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extract  # noqa: E402
import fetch  # noqa: E402


# ---------- 그림 목록 ----------

def test_lof_basic_and_multiline_title():
    text = """LIST OF FIGURES
Figure 1.2-1:
Conceptual Site Layout. . . . . . . . . . . . . 1.2-21
Figure 1.2-3:
Schematic of a Single NuScale Power Module and Associated
Secondary Equipment. . . . . . . . . . . . . 1.2-23
Figure 1.7-1a:
Electrical Symbols. . . . . . . . . . . . . 1.7-5"""
    e = extract.parse_lof_text(text)
    assert [x["fig_no"] for x in e] == ["1.2-1", "1.2-3", "1.7-1a"]
    assert e[1]["title"] == "Schematic of a Single NuScale Power Module and Associated Secondary Equipment"
    assert [x["printed_page"] for x in e] == ["1.2-21", "1.2-23", "1.7-5"]


def test_lof_same_line_entry():
    e = extract.parse_lof_text("Figure 1-2: BWRX-300 Major Systems ...................... 19")
    assert e == [{"key": "1-2", "fig_no": "1-2", "sheet": None, "title": "BWRX-300 Major Systems", "printed_page": "19"}]


def test_lof_sheets_get_separate_keys():
    text = """Figure 2.3-29 (1 of 8): Elevation Profiles . . . . . . . 2.3-45
Figure 2.3-29 (2 of 8): Elevation Profiles . . . . . . . 2.3-46"""
    e = extract.parse_lof_text(text)
    assert [(x["key"], x["fig_no"], x["sheet"]) for x in e] == [("2.3-29-s1", "2.3-29", 1), ("2.3-29-s2", "2.3-29", 2)]


def test_lof_broken_dot_leader():
    text = """Figure 2.4.12-7:
Potentiometric Surface of the Gulf
Coast Aquifer System . 2.4-402
Figure 2.5.2-45:
Hazard Spectral Acceleration 2.5-177"""
    e = extract.parse_lof_text(text)
    assert [(x["title"], x["printed_page"]) for x in e] == [
        ("Potentiometric Surface of the Gulf Coast Aquifer System", "2.4-402"),
        ("Hazard Spectral Acceleration", "2.5-177")]


def test_lof_loose_tail_needs_same_chapter():
    # 제목 끝 숫자가 다른 장 번호면 쪽 표기로 떼지 않는다
    e = extract.parse_lof_text("Figure 3.1-2:\nComparison with Table 7-1")
    assert e[0]["printed_page"] is None


def test_is_list_page():
    assert extract.is_list_page("A . . . . . 1\nB . . . . . 2\nC . . . . . 3")
    assert not extract.is_list_page("Figure 1.2-1: Site Layout\nsome body text.")


# ---------- 캡션 ----------

@pytest.mark.parametrize("line,key,title", [
    ("Figure 1.2-8: Decay Heat Removal System", "1.2-8", "Decay Heat Removal System"),
    ("Figure 1-1: Representation of BWRX-300 Standard Power Block ", "1-1", "Representation of BWRX-300 Standard Power Block"),
    ("Figure 2.3-29 (3 of 8): Elevation Profiles to 50 Mi.", "2.3-29-s3", "Elevation Profiles to 50 Mi."),
    ("Figure 1.7-3f. P&ID Legends", "1.7-3f", "P&ID Legends"),
])
def test_caption_matches(line, key, title):
    m = extract.CAPTION.match(line)
    assert m and extract.fig_key(m) == key and m.group(3).strip() == title


@pytest.mark.parametrize("line", [
    "as shown in Figure 1.2-3, the module",       # 문장 속 언급
    "Figure 2.3-29 (1-8). Figure 2.3-30 provides",  # 본문 참조 (콜론 없음)
    "Table 1.2-1: Major Parameters",
])
def test_caption_rejects_references(line):
    assert extract.CAPTION.match(line) is None


def test_text_fix_for_broken_font_encoding():
    assert "XeͲ100\x03Principal".translate(extract._FIX) == "Xe-100 Principal"


# ---------- 합성 PDF로 Withheld 판정·크롭 ----------

def _page_with_header(doc, label, w=612, h=792):
    p = doc.new_page(width=w, height=h)
    p.insert_text((72, 50), "Test Final Safety Analysis Report", fontsize=9)
    p.insert_text((290, 745), label, fontsize=9)
    p.insert_text((480, 745), "Revision 1", fontsize=9)
    return p


def _make_doc(tmp_path):
    doc = pymupdf.open()
    # 앞부분 쪽들(머리글·바닥글 반복을 만들기 위해)
    for k in range(3):
        _page_with_header(doc, f"1.1-{k + 1}").insert_text((72, 120), "Body text of the introduction.", fontsize=11)
    lof = _page_with_header(doc, "iv")
    y = 100
    for line in ["LIST OF FIGURES",
                 "Figure 1.2-1:", "Public Drawing . . . . . . . . . . . . . . 1.2-5",
                 "Figure 1.2-2:", "Hidden Drawing . . . . . . . . . . . . . . 1.2-6",
                 "Figure 1.2-3:", "Bracketed Drawing . . . . . . . . . . . . 1.2-7"]:
        lof.insert_text((72, y), line, fontsize=11)
        y += 16
    # 1.2-1: 공개 (벡터 도면 + 아래 주석)
    p = _page_with_header(doc, "1.2-5")
    p.insert_text((150, 90), "Figure 1.2-1: Public Drawing", fontsize=11)
    for k in range(5):
        p.draw_rect(pymupdf.Rect(120 + 60 * k, 150, 160 + 60 * k, 400), color=(0, 0, 1))
    p.insert_text((120, 430), "Not to Scale", fontsize=9)
    # 1.2-2: 'Withheld' 문구
    p = _page_with_header(doc, "1.2-6")
    p.insert_text((150, 90), "Figure 1.2-2: Hidden Drawing", fontsize=11)
    p.insert_text((200, 300), "{{  Withheld - See Part 9  }}", fontsize=11)
    # 1.2-3: 기밀 괄호만 남은 쪽
    p = _page_with_header(doc, "1.2-7")
    p.insert_text((80, 90), "[[", fontsize=11)
    p.insert_text((500, 600), "]]XE", fontsize=11)
    p.insert_text((150, 640), "Figure 1.2-3: Bracketed Drawing", fontsize=11)

    data = doc.tobytes()
    d = tmp_path / "docs" / "test-fsar-ch1-r1"
    d.mkdir(parents=True)
    (d / "doc.json").write_text(json.dumps({"reactor": "test", "title": "Test", "adams_no": None}))
    fetch.store(data, {"url": "https://example.invalid/t.pdf", "acquired_via": "test"}, d)
    return d


def test_end_to_end_synthetic(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    d = _make_doc(tmp_path)
    rows, info = extract.run(d)
    by = {r["fig_no"]: r for r in rows}
    assert info["lof_entries"] == 3
    assert by["1.2-1"]["status"] == "public" and by["1.2-1"]["pdf_page"] == 5
    assert by["1.2-1"]["printed_page"] == by["1.2-1"]["printed_page_lof"] == "1.2-5"
    assert by["1.2-1"]["crop_method"] == "vector"
    assert (by["1.2-2"]["status"], by["1.2-2"]["reason"]) == ("withheld", "text:Withheld")
    assert (by["1.2-3"]["status"], by["1.2-3"]["reason"]) == ("withheld", "proprietary-brackets")
    # 크롭에 캡션과 주석이 들어가고, 머리글은 빠진다
    x0, y0, x1, y1 = by["1.2-1"]["bbox_pt_unrotated"]
    assert y0 < 90 and y1 > 430 and y0 > 55
    assert (d / "figures" / "test-fsar-ch1-r1-fig-1.2-1.png").exists()
    assert not (d / "figures" / "test-fsar-ch1-r1-fig-1.2-2.png").exists()


# ---------- 저장·약칭 ----------

def test_store_appends_provenance_and_keeps_pdf_readonly(tmp_path):
    data = _tiny_pdf()
    d = tmp_path / "doc"
    d.mkdir()
    pdf, prov = fetch.store(data, {"acquired_via": "download"}, d)
    pdf2, prov2 = fetch.store(data, {"acquired_via": "import-local"}, d)
    assert pdf == pdf2 and [a["acquired_via"] for a in prov2["acquisitions"]] == ["download", "import-local"]
    assert not (pdf.stat().st_mode & 0o222)


def test_store_rejects_non_pdf(tmp_path):
    with pytest.raises(ValueError):
        fetch.store(b"<html>Access Denied</html>", {}, tmp_path)


def _tiny_pdf():
    doc = pymupdf.open()
    doc.new_page()
    return doc.tobytes()


def test_slug_from_adams():
    doc = {"DocketNumber": ["05200050"], "DocumentType": ["Final Safety Analysis Report (FSAR)"],
           "DocumentTitle": 'NuScale Power, LLC - Part 2, Chapter 1, "Introduction," Revision 2'}
    assert fetch.slug_from_adams(doc) == ("nuscale-us460-fsar-ch1-r2", None)
    assert fetch.slug_from_adams({**doc, "DocketNumber": ["09999999"]})[0] is None
    assert fetch.slug_from_adams({**doc, "DocumentType": ["Letter"]})[0] is None


def test_two_figures_on_one_page_and_repeated_logo(tmp_path, monkeypatch):
    """캡션이 그림 아래에 오는 쪽에 그림 두 개 + 모든 쪽에 같은 로고 이미지."""
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), 0)
    logo.clear_with(90)
    chart = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), 0)
    chart.clear_with(30)
    doc = pymupdf.open()
    xref = None
    for k in range(4):
        p = _page_with_header(doc, f"3.4-{k + 1}")
        r = pymupdf.Rect(30, 20, 80, 95)  # 머리글 글자보다 아래까지 내려오는 로고
        xref = p.insert_image(r, pixmap=logo) if xref is None else p.insert_image(r, xref=xref)
        if k == 0:
            p.insert_text((72, 140), "Body text before the figures.", fontsize=11)
    p = doc[3]
    p.insert_image(pymupdf.Rect(150, 130, 450, 330), pixmap=chart)
    p.insert_text((180, 350), "Figure 3.4.6-5: First Chart", fontsize=11)
    p.insert_image(pymupdf.Rect(150, 400, 450, 600), pixmap=chart)
    p.insert_text((180, 620), "Figure 3.4.6-6: Second Chart", fontsize=11)
    d = tmp_path / "docs" / "test-psar"
    d.mkdir(parents=True)
    (d / "doc.json").write_text(json.dumps({"reactor": "test", "title": "Test", "adams_no": None}))
    fetch.store(doc.tobytes(), {"url": "https://example.invalid/p.pdf"}, d)

    rows, _ = extract.run(d)
    by = {r["fig_no"]: r for r in rows}
    a, b = by["3.4.6-5"]["bbox_pt_unrotated"], by["3.4.6-6"]["bbox_pt_unrotated"]
    assert by["3.4.6-5"]["status"] == by["3.4.6-6"]["status"] == "public"
    assert a[1] > 95 and b[1] > 95          # 로고(y≤95)가 크롭에 들어가지 않음
    assert a[3] < 400 and b[1] > 340         # 첫 그림은 둘째 그림 전에서, 둘째는 첫 캡션 뒤에서 끊김
    assert a[3] > 350 and b[3] > 620         # 각자 자기 캡션은 포함


# ---------- 리뷰 반영 ----------

def _two_figure_doc(second_withheld, third=True):
    doc = pymupdf.open()
    for k in range(4):
        _page_with_header(doc, f"3.4-{k + 1}")
    p = doc[3]
    for k in range(5):
        p.draw_rect(pymupdf.Rect(120 + 60 * k, 120, 160 + 60 * k, 300), color=(0, 0, 1))
    p.insert_text((180, 330), "Figure 3.4-5: Public Chart", fontsize=11)
    if second_withheld:
        p.insert_text((200, 450), "{{ Withheld - See Part 9 }}", fontsize=11)
    else:
        for k in range(5):
            p.draw_rect(pymupdf.Rect(120 + 60 * k, 380, 160 + 60 * k, 560), color=(1, 0, 0))
    p.insert_text((180, 600), "Figure 3.4-6: Second Chart", fontsize=11)
    if third:
        q = doc[2]
        for k in range(5):
            q.draw_rect(pymupdf.Rect(120 + 60 * k, 120, 160 + 60 * k, 300))
        q.insert_text((180, 330), "Figure 3.4-7: Third Chart", fontsize=11)
    return doc.tobytes()


def _fresh_dir(tmp_path, data):
    d = tmp_path / "docs" / "t"
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("*.pdf"):
        p.chmod(0o644)
        p.unlink()
    for p in d.glob("*.json"):
        if p.name != "doc.json":
            p.unlink()
    (d / "doc.json").write_text(json.dumps({"reactor": "t", "title": "t"}))
    fetch.store(data, {"url": "x"}, d)
    return d


def test_withheld_judged_per_figure_not_per_page(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    rows, _ = extract.run(_fresh_dir(tmp_path, _two_figure_doc(second_withheld=True)))
    by = {r["fig_no"]: r for r in rows}
    assert by["3.4-5"]["status"] == "public"
    assert (by["3.4-6"]["status"], by["3.4-6"]["reason"]) == ("withheld", "text:Withheld")


def test_rerun_removes_stale_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    d = _fresh_dir(tmp_path, _two_figure_doc(second_withheld=False))
    extract.run(d)
    assert (d / "figures" / "t-fig-3.4-6.png").exists() and (d / "figures" / "t-fig-3.4-7.json").exists()
    d = _fresh_dir(tmp_path, _two_figure_doc(second_withheld=True, third=False))
    extract.run(d)
    left = sorted(p.name for p in (d / "figures").iterdir())
    assert left == ["t-fig-3.4-5.json", "t-fig-3.4-5.png", "t-fig-3.4-6.json"]


def test_run_skips_broken_folder(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    with pytest.raises(extract.SkipDoc):
        extract.run(d)
    (d / "doc.json").write_text("{}")
    with pytest.raises(extract.SkipDoc, match="PDF 0개"):
        extract.run(d)


def test_doc_dir_refuses_other_document_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "DOCS", tmp_path)
    fetch.doc_dir("xe100-tr", {"adams_no": "ML22195A260", "title": "old"})
    with pytest.raises(ValueError, match="ML22195A260"):
        fetch.doc_dir("xe100-tr", {"adams_no": "ML21288A173", "title": "new"})
    assert json.loads((tmp_path / "xe100-tr" / "doc.json").read_text())["title"] == "old"
    fetch.doc_dir("xe100-tr", {"adams_no": "ML22195A260", "title": "again"})  # 같은 문서는 다시 받기 허용


@pytest.mark.parametrize("status,allowed", [(200, True), (404, True), (401, False), (403, False)])
def test_robots_status_rules(status, allowed):
    rp = fetch.parse_robots(status, "User-agent: *\nDisallow: /private/\n")
    assert rp.can_fetch(fetch.AGENT, "https://example.org/docs/a.pdf") is allowed
    assert not fetch.parse_robots(200, "User-agent: *\nDisallow: /docs/\n").can_fetch(fetch.AGENT, "https://e.org/docs/a")


def test_html_block_page_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("SMR_CONTACT_EMAIL", "test@example.org")
    monkeypatch.setattr(fetch, "MIN_INTERVAL", 0)
    monkeypatch.setattr(fetch, "_robots", {})

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, html="<html>Access Denied</html>")
    with fetch.client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(fetch.Blocked, match="PDF가 아닌 응답"):
            fetch.download(c, "https://example.org/a.pdf", tmp_path)
    assert not list(tmp_path.iterdir())


def test_client_requires_contact(monkeypatch):
    monkeypatch.delenv("SMR_CONTACT_EMAIL", raising=False)
    with pytest.raises(SystemExit):
        fetch.client()


def test_repeated_withheld_marker_near_edge_keeps_reason(tmp_path, monkeypatch):
    """여러 쪽 같은 자리에 반복되는 '{{ Withheld }}' 가 머리글로 분류돼도 판정 이유는 text:Withheld."""
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    doc = pymupdf.open()
    for k in range(4):
        p = _page_with_header(doc, f"1.2-{k + 1}")
        p.insert_text((150, 90), f"Figure 1.2-{k + 1}: Building Elevation {k}", fontsize=11)
        p.insert_text((350, 105), "{{  Withheld - See Part 9  }}", fontsize=9)
    rows, _ = extract.run(_fresh_dir(tmp_path, doc.tobytes()))
    assert {(r["status"], r["reason"]) for r in rows} == {("withheld", "text:Withheld")}
