"""framework.core.spec_sources: documents. HTML pages, PDF layout text and
plain text become normalised text plus label/value pairs; Amazon content is
refused at this layer too. Uses the real trimmed fixtures; the one PDF test
builds a tiny PDF in memory and needs poppler's pdftotext (skipped when it
is not installed)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from framework.core import spec_sources as ss

FX = Path(__file__).resolve().parent / "fixtures" / "spec_sources"
SOURCES = {d["id"]: d for d in json.loads((FX / "SOURCES.json").read_text())["documents"]}


def load(doc_id: str) -> ss.SpecDocument:
    s = SOURCES[doc_id]
    raw = (FX / s["file"]).read_text()
    if s["format"] == "html":
        return ss.document_from_html(raw, url=s["url"], kind=s["kind"], fetched_at=s["fetched_on"])
    return ss.document_from_text(raw, url=s["url"], kind=s["kind"], content_type="application/pdf",
                                 fetched_at=s["fetched_on"])


def pairs(doc):
    return {(p.label, p.value) for p in doc.pairs}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def test_key_value_spans_become_pairs():
    doc = load("jackery-product-page")
    got = pairs(doc)
    assert ("Capacity", "30.4Ah/35.2V DC (1070 Wh)") in got
    assert ("AC Total Output", "1500W Rated, 3000W Surge peak") in got
    assert ("Cell Chemistry", "LiFePO4") in got
    dc = [v for (l, v) in got if l == "DC Input"]
    assert dc and dc[0].startswith("(2x) DC 8mm Ports: 12-16V(Working Voltage)")
    # group headings never pair with a whole group of rows
    assert not [l for (l, _) in got if l in ("General", "Battery", "Output Ports", "SolarSaga 200W")]
    assert doc.title == "Jackery Portable Power Station Explorer 1000 v2"
    assert doc.source.kind == "manufacturer-page" and len(doc.content_sha256) == 64


def test_table_rows_become_pairs():
    got = pairs(load("jackery-faq"))
    assert ("Product Model", "JE-1000D") in got
    assert ("Type of Battery Used", "LiFePO4") in got           # LiFePO₄ normalised
    assert ("Temperature Range for Product Charging", "0 °C ~ 45 °C") in got   # ℃ normalised


def test_grid_rows_sections_colon_lines_and_jsonld():
    doc = load("fossibot-product-page")
    by_label = {p.label: p for p in doc.pairs}
    assert by_label["Surge Power"].value == "4800W"
    assert by_label["Surge Power"].section == "OUTPUT"
    assert by_label["Capacity"].section == "BATTERY INFO"
    assert by_label["Work Voltage"].origin == "colon-line" and by_label["Work Voltage"].value == "11.5-50V"
    assert by_label["mpn"].origin == "jsonld" and by_label["brand"].value == "FOSSiBOT"
    assert "10ms uninterruptible home backup UPS;" in doc.lines


def test_definition_lists_from_another_niche():
    got = pairs(load("ecovacs-spec"))
    assert ("Dust Bin Capacity (ml)", "420") in got
    assert ("Noise Value (dBA )", "65/66/71/74") in got
    assert ("sku", "110-2241-0104") in got


def test_manual_mirror_tables():
    got = pairs(load("fossibot-manual"))
    assert ("MPPT Input", "11.5-50V/20A/500W max") in got
    assert ("Dimension", "L*W*H=396X284X321mm") in got


def test_malformed_and_deep_markup_does_not_break_parsing():
    deep = "<div>" * 5000 + "<span>Weight</span><span>12 kg</span>" + "</div>" * 10
    doc = ss.document_from_html(deep, url="https://maker.example/p", kind="manufacturer-page")
    assert ("Weight", "12 kg") in pairs(doc)
    messy = ("<table><tr><td>Capacity<td>500Wh<tr><td>Weight<td>6 kg</table>"
             "<ul><li>One<li>Two</ul><p>para<div>block</div><dl><dt>Noise<dd>45 dB<dt>Size<dd>1x2x3 cm</dl>")
    got = pairs(ss.document_from_html(messy, url="https://maker.example/p", kind="manufacturer-page"))
    assert {("Capacity", "500Wh"), ("Weight", "6 kg"), ("Noise", "45 dB"), ("Size", "1x2x3 cm")} <= got


def test_site_chrome_outside_main_is_ignored():
    # A store's mega menu lists other models with their own specs; it must not
    # be read as this product's specs (seen on a real store page).
    body = "".join(f"<tr><td>Row {i}</td><td>{i} W</td></tr>" for i in range(30))
    html = ("<body><header><div><span>Capacity</span><span>2048Wh</span></div></header>"
            "<nav><div><span>Weight</span><span>99 kg</span></div></nav>"
            f"<main><h1>Station</h1><table><tr><td>Capacity</td><td>1070Wh</td></tr>{body}</table></main>"
            "<footer><div><span>Capacity</span><span>3600Wh</span></div></footer></body>")
    got = pairs(ss.document_from_html(html, url="https://maker.example/p", kind="manufacturer-page"))
    caps = {v for (l, v) in got if l == "Capacity"}
    assert caps == {"1070Wh"} and not any(l == "Weight" for (l, _) in got)
    # without <main>, nav and footer are still dropped; a <main> that is nearly empty is ignored
    no_main = html.replace("<main>", "<div>").replace("</main>", "</div>")
    caps = {v for (l, v) in pairs(ss.document_from_html(no_main, url="https://maker.example/p",
                                                        kind="manufacturer-page")) if l == "Capacity"}
    assert caps == {"1070Wh", "2048Wh"}
    shell_main = html.replace("<main>", "<main></main><div>").replace("</main>", "</div>", 1)
    assert "1070Wh" in {v for (l, v) in pairs(ss.document_from_html(
        shell_main, url="https://maker.example/p", kind="manufacturer-page")) if l == "Capacity"}


def test_spec_table_nested_in_a_layout_table():
    html = ("<table><tr><td>Specifications</td><td><table><tr><td>Capacity</td><td>500Wh</td></tr>"
            "<tr><td>Weight</td><td>6 kg</td></tr></table></td></tr></table>")
    doc = ss.document_from_html(html, url="https://maker.example/p", kind="manufacturer-page")
    assert pairs(doc) == {("Capacity", "500Wh"), ("Weight", "6 kg")}


def test_scripts_styles_and_svg_are_not_text():
    html = ("<html><head><title>T</title><style>.a{}</style></head><body><script>var s='Capacity: 9Wh'</script>"
            "<svg><text>Capacity: 8Wh</text></svg><p>Capacity: 7Wh</p></body></html>")
    doc = ss.document_from_html(html, url="https://maker.example/p", kind="manufacturer-page")
    assert doc.lines == ["Capacity: 7Wh"]


def test_a_javascript_shell_is_flagged_little_text():
    shell = '<!doctype html><html><head><title>Support</title><script src="/app.js"></script></head><body><div id="root"></div></body></html>'
    doc = ss.document_from_html(shell, url="https://support.maker.example/specs", kind="manufacturer-faq")
    assert "little-text" in doc.flags and doc.pairs == ()


def test_unknown_source_kind_is_rejected():
    with pytest.raises(ValueError):
        ss.document_from_html("<p>x</p>", url="https://maker.example", kind="forum-post")


# ---------------------------------------------------------------------------
# Layout text (PDF manuals)
# ---------------------------------------------------------------------------

def test_column_table_in_a_pdf_manual():
    doc = load("pecron-manual")
    labels = [ss.re.compile(x, ss.re.I) for x in (r"^capacity$", r"^battery type$", r"^net weight$")]
    cols = {p.label: p.value for p in doc.layout_pairs(labels)}
    assert cols["Capacity"] == "1920Wh (25.6V75Ah)"
    assert cols["Battery Type"] == "LiFePO4 Battery Retain 80%+ Original Capacity At 3500 Cycles"
    assert cols["Charging Time"] == "3.5~4H (Standard) 1.5~2H (Fast Charging)"
    assert cols["Temperature"] == "0°C~45°C/ 32°F~113°F (Charging) -20°C~45°C/ -4°F~113°F (Discharging)"
    assert cols["Dimension"] == "L16.1*W10*H11.6 in L409*W256*H297 mm"
    assert cols["Net Weight"] == "Around 48.5lb Around 22Kg"
    # one matching label is not enough to call a line a table header
    assert doc.layout_pairs(labels[:1]) == []


def test_two_column_manual_layout_with_continuation_lines():
    doc = load("jackery-eu-manual")
    got = {p.label: p.value for p in doc.pairs if p.origin == "spaced-columns"}
    assert got["Model No."] == "JE-1000F"
    assert got["Capacity"] == "1024 Wh (20Ah/51.2V DC)"
    assert got["2 × DC8020 Ports"] == "11V-16V⎓8A Max, Double to 8A Max 16V-60V⎓12A Max, Double to 21A/ 400W Max"
    assert {p.section for p in doc.pairs if p.label == "Capacity"} == {"GENERAL INFO"}


def test_text_lines_align_with_layout_lines():
    doc = load("pecron-manual")
    assert len(doc.lines) == len(doc.layout_text.split("\n"))


def _tiny_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler pdftotext not installed")
def test_real_pdf_through_pdftotext():
    doc = ss.document_from_pdf(_tiny_pdf("Rated Power: 1500W"), url="https://cdn.maker.example/manual.pdf")
    assert doc.content_type == "application/pdf" and doc.kind == "manual-pdf"
    assert ("Rated Power", "1500W") in pairs(doc)
    assert len(doc.content_sha256) == 64


def test_outlined_pdf_without_a_text_layer_is_flagged():
    # The official US-edition manual for the same station is outlined: pdftotext returns form feeds only.
    doc = ss.document_from_pdf(b"%PDF-1.5 ...", url="https://cdn.maker.example/us-manual.pdf",
                               converter=lambda b: "\f" * 20)
    assert {"no-text-layer", "little-text"} <= doc.flags and doc.pairs == ()


def test_missing_pdftotext_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))            # a host without poppler-utils
    with pytest.raises(ss.PdfTextUnavailable):
        ss.pdf_to_text(b"%PDF-1.4")


@pytest.mark.parametrize("cmd", [
    ["definitely-not-a-real-binary-xyz", "-", "-"],      # any other program
    ["/tmp/pdftotext", "-layout", "-", "-"],             # a path, not the PATH lookup
    ["sh", "-c", "id", "-", "-"],
    ["pdftotext", "-layout", "-opw", "x", "-", "-"],     # flags outside the allowlist
    ["pdftotext", "-layout", "/etc/passwd", "-"],        # must read stdin, write stdout
    [],
])
def test_pdf_command_from_config_is_held_to_pdftotext(cmd):
    with pytest.raises(ss.SpecSourcesError) as e:
        ss.pdf_to_text(b"%PDF-1.4", cfg={"pdf_command": cmd})
    assert not isinstance(e.value, ss.PdfTextUnavailable)
    engine = ss.SpecSources(ss.resolve_config(file_cfg={}, site_cfg={"documents": {"pdf_command": cmd}}))
    assert any("pdf_command" in p for p in engine.problems)
    # an injected converter replaces the command entirely
    assert not ss.SpecSources(ss.resolve_config(file_cfg={}, site_cfg={"documents": {"pdf_command": cmd}}),
                              pdf_converter=lambda b: "").problems


# ---------------------------------------------------------------------------
# Amazon refusal at the document layer
# ---------------------------------------------------------------------------

def test_amazon_page_is_refused_even_under_a_harmless_url():
    page = (FX / "amazon" / "synthetic-amazon-detail-page.html").read_text()
    with pytest.raises(ss.AmazonContentRefused):
        ss.document_from_html(page, url="https://mirror.example/listing", kind="manufacturer-page")


def test_page_markers_without_canonical_still_refused():
    page = (FX / "amazon" / "synthetic-amazon-detail-page.html").read_text()
    page = "\n".join(l for l in page.splitlines() if "canonical" not in l and "Best Sellers" not in l)
    with pytest.raises(ss.AmazonContentRefused, match="amazon-page-markers"):
        ss.document_from_html(page, url="https://mirror.example/listing", kind="manufacturer-page")


@pytest.mark.parametrize("text", ["Best Sellers Rank: #3 in Generators", "ASIN : B0D91KV51C",
                                  "ASIN | B0D91KV51C", "Customers who bought this item also bought"])
def test_amazon_text_markers_refuse_text_documents(text):
    with pytest.raises(ss.AmazonContentRefused):
        ss.document_from_text(f"Capacity: 1000Wh\n{text}\n", url="https://notes.example/x", kind="manual-entry")


def test_amazon_url_refused_for_any_content():
    with pytest.raises(ss.AmazonContentRefused):
        ss.document_from_html("<p>Capacity: 1000Wh</p>", url="https://www.amazon.com/dp/B0D91KV51C",
                              kind="manufacturer-page")
    with pytest.raises(ss.AmazonContentRefused):
        ss.document_from_pdf(b"%PDF", url="https://m.media-amazon.com/images/I/manual.pdf",
                             converter=lambda b: "Capacity 1000Wh")


@pytest.mark.parametrize("doc_id", sorted(SOURCES))
def test_real_manufacturer_fixtures_are_not_mistaken_for_amazon(doc_id):
    s = SOURCES[doc_id]
    raw = (FX / s["file"]).read_text()
    assert ss.AmazonRefusal().reason(url=s["url"], html=raw if s["format"] == "html" else "",
                                     text=raw) == ""
    assert load(doc_id).lines
