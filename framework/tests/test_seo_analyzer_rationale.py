"""Rec text honesty for both SEO analyzer copies (2026-10-01 audit).

The trust-signal rule told sites to show merchant claims that "don't need
to be true", and several rationale strings asserted things about a
competitor that were false on the live site, plus a FAQ rich-result CTR
promise Google withdrew for commercial sites in August 2023."""
from __future__ import annotations

import importlib.util as iu
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_COPIES = {
    "seo-opportunity-agent": _ROOT / "agents" / "seo-opportunity-agent" / "lib" / "analyzer" / "analyzer.py",
    "seo-analyzer": _ROOT / "agents" / "seo-analyzer" / "analyzer.py",
}
_BANNED = [
    "don't need to be true", "'full warranty'", "'30-day return policy'",
    "schema penalty", "rich-snippet", "10-question faq", "studio-supplies",
]


def _load(name: str):
    spec = iu.spec_from_file_location(f"analyzer_{name.replace('-', '_')}", _COPIES[name])
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ids():
    n = iter(range(1, 1000))
    return lambda: f"rec-{next(n):03d}"


def _write_pages(tmp_path: Path, pages: list[dict]) -> Path:
    (tmp_path / "pages-by-type.jsonl").write_text("\n".join(json.dumps(p) for p in pages))
    return tmp_path


def _text(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False).lower()


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_trust_signal_rule_asks_only_for_verifiable_facts(tmp_path, name):
    mod = _load(name)
    data = _write_pages(tmp_path, [
        {"url": f"https://s.test/p/{i}", "status": 200, "type": "product",
         "trust_signal_count": 0} for i in range(6)])
    recs: list = []
    mod._add_trust_signal_density_recs(data, recs, _ids(), 50)
    assert len(recs) == 1
    txt = _text(recs[0])
    for phrase in _BANNED:
        assert phrase not in txt, phrase
    assert "never add" in txt and "affiliate disclosure" in txt


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_faq_recs_use_one_target_and_no_rich_result_promise(tmp_path, name):
    mod = _load(name)
    pages = [{"url": f"https://s.test/p/{i}", "status": 200, "type": "product",
              "jsonld_types": ["FAQPage"], "faq_question_count": 2, "faq_avg_answer_words": 12,
              "faq_short_answer_count": 2} for i in range(4)]
    data = _write_pages(tmp_path, pages)
    recs: list = []
    mod._add_faq_quality_recs(data, recs, _ids(), 50)
    assert recs, "thin-FAQ rule should fire"
    txt = _text(recs[0])
    assert "4-6" in txt and "40-80" in txt
    assert recs[0]["expected_impact"]["metric"] == "ai_referral_traffic"
    for phrase in _BANNED:
        assert phrase not in txt, phrase


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_footer_rationale_is_neutral(tmp_path, name):
    mod = _load(name)
    (tmp_path / "site-signals.json").write_text(json.dumps(
        {"homepage": {"footer_trust_links": {"privacy": True, "terms": False}}}))
    recs: list = []
    mod._add_footer_trust_links_recs(tmp_path, recs, _ids(), 50)
    assert recs and "studio-supplies" not in _text(recs[0])
    assert "methodology" in _text(recs[0])


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_source_has_no_banned_claims(name):
    src = _COPIES[name].read_text().lower()
    for phrase in ("don't need to be true", "schema penalty", "10-question faq",
                   "rich-snippet expandable", "aim for 5-10 q&as",
                   "5 questions × ~80 words"):
        assert phrase not in src, phrase
