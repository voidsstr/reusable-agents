"""framework.core.content_policy — opt-in, negation-aware rule sets (build item F6a).

The repo default opts no site in, so every existing site is unaffected. The
electrical / electrical-year1 / medical rule sets are config (the power-site
design's binding amendments 1 and 8); these tests pin their behaviour on
allowed and rejected fixtures: 'never backfeed' passes, 'backfeed through the
dryer outlet' fails, a short safety callout may name banned practices but not
give steps, the licensed-electrician notice is required whenever whole-home,
furnace or sump connection is discussed, CPAP and oxygen runtime claims fail,
and 240 V / L14-30 / L5-30 / 14-50 cords fail. No network, no DB, no storage
writes.
"""
from __future__ import annotations

import copy
import json
import pathlib
import re

import pytest

from framework.core import content_policy as cp
from framework.core.storage import LocalFilesystemStorage

FIX = pathlib.Path(__file__).parent / "fixtures" / "content_policy"
ALL_SETS = ["electrical", "electrical-year1", "medical"]


@pytest.fixture(scope="module")
def repo_cfg() -> dict:
    return cp.load_config()


@pytest.fixture(scope="module")
def pol(repo_cfg):
    return cp.resolve_policy(rule_sets=ALL_SETS, config=repo_cfg)


def _rejects(audit) -> list[str]:
    return sorted({v.rule_id for v in audit.rejects})


# ── the default is inert ─────────────────────────────────────────────────────

def test_repo_default_opts_no_site_in(repo_cfg):
    assert repo_cfg["default"]["rule_sets"] == []
    assert repo_cfg["sites"] == {}
    for key in json.loads((pathlib.Path(cp.__file__).resolve().parents[2] / "config"
                           / "article-link-guard-config.json").read_text())["sites"]:
        p = cp.resolve_policy(key, config=repo_cfg)          # every existing site key
        assert not p.active and p.rule_sets == ()
        audit = cp.check_article(p, body_md="Backfeed through the dryer outlet with a double-male cord.")
        assert audit.passes and audit.violations == []


def test_rule_sets_are_defined_and_compile(repo_cfg):
    assert set(ALL_SETS) <= set(cp.defined_rule_sets(repo_cfg))
    for name in cp.defined_rule_sets(repo_cfg):
        rules, notices = cp.compile_rule_set(name, repo_cfg["rule_sets"][name])
        assert rules or notices
        for r in rules:
            assert r.why and r.fix, f"{r.id} needs why/fix text for the author addendum"


def test_a_site_gets_only_the_sets_it_opts_into(repo_cfg):
    cfg = cp.merge_config(repo_cfg, {"sites": {"powersite": {"rule_sets": ["medical"]}}})
    p = cp.resolve_policy("powersite-article-proposal-agent", config=cfg)
    assert p.site_key == "powersite" and p.rule_sets == ("medical",)
    body = "Backfeed through the dryer outlet. A 1,000 Wh station runs a CPAP for two nights."
    assert _rejects(cp.check_markdown(p, body)) == ["medical.medical-runtime-claim"]
    assert not cp.resolve_policy("othersite", config=cfg).active


def test_storage_copy_opts_a_site_in_and_merges_per_block(tmp_path, repo_cfg):
    s = LocalFilesystemStorage(str(tmp_path))
    s.write_json(cp.CONFIG_KEY, {
        "sites": {"powersite": {"rule_sets": ["electrical"], "rule_overrides": {"electrical.bms-bypass": "warn"}}},
        "rule_sets": {"house-style": {"rules": [{"id": "no-hype", "patterns": ["\\bgame[- ]changer\\b"],
                                                  "why": "w", "fix": "f", "action": "warn"}]}},
    })
    cfg = cp.load_config(s)
    assert "electrical" in cfg["rule_sets"] and "house-style" in cfg["rule_sets"]   # sets merged, not replaced
    p = cp.resolve_policy("powersite", config=cfg)
    a = cp.check_markdown(p, "You can bypass the BMS with a jumper.")
    assert a.passes and [v.rule_id for v in a.warnings] == ["electrical.bms-bypass"]
    assert cp.resolve_policy("powersite", storage=s).rule_sets == ("electrical",)


def test_rule_overrides_retune_or_drop_rules(repo_cfg):
    cfg = cp.merge_config(repo_cfg, {"sites": {"powersite": {
        "rule_sets": ALL_SETS, "rule_overrides": {"medical": "off", "electrical-year1.240v-mention": "off"}}}})
    p = cp.resolve_policy("powersite", config=cfg)
    assert not any(r.rule_set == "medical" for r in p.rules)
    assert "electrical-year1.240v-mention" not in {r.id for r in p.rules}
    assert cp.check_markdown(p, "A 1,000 Wh station runs a CPAP for two nights.").passes


@pytest.mark.parametrize("over, msg", [
    ({"sites": {"x": {"rule_sets": ["electrcal"]}}}, "not defined"),
    ({"sites": {"x": {"rule_sets": ["bad"]}}, "rule_sets": {"bad": {"rules": [{"id": "r", "patterns": ["(unclosed"]}]}}},
     "invalid regex"),
    ({"sites": {"x": {"rule_sets": ["bad"]}}, "rule_sets": {"bad": {"rules": [{"id": "r", "patterns": ["x"],
                                                                                "action": "block"}]}}}, "action"),
    ({"sites": {"x": {"rule_sets": ["bad"]}}, "rule_sets": {"bad": {"rules": [{"patterns": ["x"]}]}}}, "needs an id"),
    ({"sites": {"x": {"rule_sets": ["bad"]}}, "rule_sets": {"bad": {"rules": [{"id": "r", "patterns": ["x"],
                                                                                "fields": ["sidebar"]}]}}},
     "unknown field"),
    ({"sites": {"x": {"rule_sets": ["electrical"], "rule_overrides": {"electrical.backfeed": "maybe"}}}}, "action"),
])
def test_bad_config_fails_closed(repo_cfg, over, msg):
    with pytest.raises(cp.ContentPolicyConfigError, match=msg):
        cp.resolve_policy("x", config=cp.merge_config(repo_cfg, over))


def test_explicit_config_file_and_unreadable_file(tmp_path, repo_cfg):
    f = tmp_path / "cfg.json"
    f.write_text(json.dumps({"sites": {"powersite": {"rule_sets": ["medical"]}}}))
    assert cp.resolve_policy("powersite", config=cp.load_config(None, path=f)).rule_sets == ("medical",)
    with pytest.raises(cp.ContentPolicyConfigError):
        cp.load_config(None, path=tmp_path / "missing.json")


def test_site_block_prefers_the_longest_key():
    cfg = {"sites": {"picks": {"rule_sets": []}, "powerpicks": {"rule_sets": ["medical"]}}}
    assert cp.site_block(cfg, "powerpicks-article-proposal-agent")[0] == "powerpicks"
    assert cp.site_block(cfg, "https://www.powerpicks.example")[0] == "powerpicks"
    assert cp.site_block(cfg, "")[0] == ""


# ── fixtures: sentences and articles ────────────────────────────────────────

SENTENCES = json.loads((FIX / "sentences.json").read_text())["cases"]
ARTICLES = json.loads((FIX / "articles.json").read_text())["cases"]


@pytest.mark.parametrize("case", SENTENCES, ids=[c["text"][:50] for c in SENTENCES])
def test_sentence_fixtures(pol, case):
    a = cp.check_markdown(pol, case["text"])
    patterns = [v.rule_id for v in a.rejects if v.kind == "pattern"]
    if case["expect"] == "pass":
        assert patterns == [], (patterns, [(e.rule_id, e.reason) for e in a.exemptions])
    else:
        assert case["rule"] in patterns, (patterns, [(e.rule_id, e.reason) for e in a.exemptions])


@pytest.mark.parametrize("case", ARTICLES, ids=[c["id"] for c in ARTICLES])
def test_article_fixtures(pol, case):
    a = cp.check_article(pol, body_md=case.get("body_md", ""), title=case.get("title", ""),
                         subtitle=case.get("subtitle", ""), excerpt=case.get("excerpt", ""), faqs=case.get("faqs"))
    assert _rejects(a) == sorted(case["expect_rejects"]), [(v.rule_id, v.excerpt) for v in a.violations]
    assert a.passes == (case["expect_rejects"] == [])


def test_the_designs_own_negation_cases(pol):
    assert cp.check_markdown(pol, "Never backfeed.").passes
    a = cp.check_markdown(pol, "Backfeed through the dryer outlet.")
    assert "electrical.backfeed" in _rejects(a) and not a.passes


# ── negation, clauses and callouts ──────────────────────────────────────────

@pytest.mark.parametrize("sentence, word, reason", [
    ("Never backfeed your panel.", "backfeed", "prohibition:never"),
    ("You should never, ever backfeed.", "backfeed", "prohibition:never"),
    ("There are no double-male cords here.", "double-male", "negation:no"),
    ("You don't have to backfeed anything.", "backfeed", "permission-negated"),
    ("Backfeeding can electrocute line workers.", "Backfeeding", "hazard"),
    ("If someone backfeeds a house, line workers can be killed.", "backfeeds", "cautionary-conditional"),
    ("Use the station's outlets rather than backfeeding.", "backfeeding", "prohibition:rather than"),
    ("You don't need an electrician to backfeed.", "backfeed", ""),
    ("It's never been easier to backfeed.", "backfeed", ""),
    ("It's not dangerous to backfeed.", "backfeed", ""),
    ("A station that never needs gas can backfeed your panel.", "backfeed", ""),
    ("If you lack an inlet, backfeed through the dryer outlet.", "backfeed", ""),
])
def test_negation_reason(pol, sentence, word, reason):
    i = sentence.index(word)
    # the electrical rule set's own vocabulary ("electrocute", "backfeed" as
    # a verb) comes with the policy; the engine default is generic English
    assert cp.negation_reason(sentence, i, i + len(word), pol.lexicon) == reason


def test_clause_bounds_split_new_clauses_not_lists():
    s = "Never use double-male cords, dryer adapters or backfeed kits"
    assert cp.clause_bounds(s) == [(0, len(s))]
    s2 = "If you lack an inlet, backfeed through the dryer outlet"
    assert [s2[a:b].strip() for a, b in cp.clause_bounds(s2)] == ["If you lack an inlet",
                                                                    "backfeed through the dryer outlet"]
    s3 = "Never backfeed; plug in directly — it is safer, but slower"
    assert [s3[a:b].strip() for a, b in cp.clause_bounds(s3)] == ["Never backfeed", "plug in directly",
                                                                    "it is safer,", "but slower"]


def test_callout_exempts_mentions_not_instructions(pol):
    mention = '> **Safety:** Backfeeding, double-male cords and dryer-outlet hookups can kill line workers.\n'
    a = cp.check_markdown(pol, mention)
    assert a.passes and {e.reason for e in a.exemptions} == {"callout"}
    assert not cp.check_markdown(pol, "> **Safety:** Backfeed only with the main breaker off.\n").passes
    # an unlabelled quote is not a callout
    assert not cp.check_markdown(pol, "> Backfeeding and double-male cords are popular hacks.\n").passes


def test_oversized_callout_is_plain_text_and_noted(pol):
    long = "> **Safety:** Backfeeding is common. " + "Words about outages and storms. " * 30 + "\n"
    a = cp.check_markdown(pol, long)
    assert "electrical.backfeed" in _rejects(a)
    assert a.notes and "longer than 700" in a.notes[0]


def test_callout_label_and_size_are_config(repo_cfg):
    cfg = cp.merge_config(repo_cfg, {"callouts": {"markdown_label_re": r"^\W*heads up\b", "max_chars": 40},
                                     "sites": {"s": {"rule_sets": ["electrical"]}}})
    p = cp.resolve_policy("s", config=cfg)
    # a bare mention (no prohibition, no hazard predicate) passes only inside a callout
    assert cp.check_markdown(p, "> Heads up: double-male cords.\n").passes
    assert not cp.check_markdown(p, "> Safety: double-male cords.\n").passes
    assert not cp.check_markdown(p, "> Heads up: double-male cords " + "and more " * 6 + ".\n").passes


def test_negation_lexicon_can_be_extended(repo_cfg):
    cfg = cp.merge_config(repo_cfg, {"negation": {"extra_strong_cues": [r"jamais"]},
                                     "sites": {"s": {"rule_sets": ["electrical"]}}})
    p = cp.resolve_policy("s", config=cfg)
    assert cp.check_markdown(p, "Jamais backfeed a house.").passes
    with pytest.raises(cp.ContentPolicyConfigError):
        cp.resolve_policy("s", config=cp.merge_config(cfg, {"negation": {"extra_strong_cues": ["(bad"]}}))


# ── notices ──────────────────────────────────────────────────────────────────

def test_notice_triggers_and_satisfaction(pol):
    trig = "A 1,500 Wh station starts most sump pumps."
    a = cp.check_markdown(pol, trig)
    assert _rejects(a) == ["electrical.licensed-electrician-notice"]
    v = a.rejects[0]
    assert v.kind == "notice" and "sump" in v.matched.lower() and "licensed electrician" in v.fix
    ok = trig + "\n\nA hard-wired pump needs an inlet installed by a licensed electrician."
    assert cp.check_markdown(pol, ok).passes
    bad = trig + "\n\nYou don't need a licensed electrician for this."
    assert not cp.check_markdown(pol, bad).passes


def test_notice_is_not_triggered_by_unrelated_hard_wiring(pol):
    assert cp.check_markdown(pol, "Prefer hard-wired Ethernet for each rig at the LAN party.").passes
    assert cp.check_markdown(pol, "A whole-home mesh router draws 15 W.").passes
    assert not cp.check_markdown(pol, "The furnace blower is hard-wired into the panel.").passes


# ── fields, tables, FAQs, headings ──────────────────────────────────────────

def test_title_only_rule_ignores_the_body(pol):
    body_only = cp.check_article(pol, title="Best Stations for Cold Garages",
                                 body_md="Furnaces are hard-wired; a licensed electrician installs any inlet.")
    assert body_only.passes
    titled = cp.check_article(pol, title="Can a Station Run Your Furnace?",
                              body_md="A licensed electrician installs any inlet.")
    assert _rejects(titled) == ["electrical-year1.furnace-article"]


def test_table_cells_take_their_column_unit():
    assert cp.table_row_text(["CPAP", "40", "25"], ["Load", "Watts", "Hours"]) == "CPAP | 40 Watts | 25 Hours"
    assert cp.table_row_text(["Fridge", "$129", "about 6"], ["Load", "Price", "Hours"]) == "Fridge | $129 Price | about 6"


def test_faq_entries_accept_the_shapes_sites_store():
    shapes = [[{"question": "Q?", "answer": "A."}], json.dumps([{"q": "Q?", "a": "A."}]),
              [{"name": "Q?", "acceptedAnswer": {"text": "A."}}]]
    for s in shapes:
        assert cp.faq_entries(s) == [("Q?", "A.")]
    assert cp.faq_entries("not json") == [] and cp.faq_entries(None) == []


def test_markdown_blocks_shapes():
    md = ("# Title\n\nIntro line one\ncontinues here.\n\n- item one\n  wrapped\n- item two\n\n"
          "| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode here\n```\n\n> **Safety:** short.\n")
    blocks = cp.markdown_blocks(md)
    kinds = [(b.kind, b.text) for b in blocks]
    assert kinds == [("heading", "Title"), ("paragraph", "Intro line one continues here."),
                     ("list_item", "item one wrapped"), ("list_item", "item two"),
                     ("table_row", "a | b"), ("table_row", "1 a | 2 b"),
                     ("code", "code here"), ("quote", "Safety: short.")]
    assert blocks[-1].callout and blocks[1].ancestors == (0,)


def test_inline_text_keeps_anchor_text_drops_urls():
    t = cp.inline_text("Read **[the backfeed guide](/guides/never-backfeed)** and ![a dryer outlet](/x.png) `code`.")
    assert t == "Read the backfeed guide and a dryer outlet code."
    assert "never-backfeed" not in t


# ── the addendum the author receives ────────────────────────────────────────

def test_addendum_names_every_violation_with_why_and_fix(pol):
    a = cp.check_article(pol, title="Whole-House Power",
                         body_md="If you don't have a transfer switch, backfeed through the dryer outlet.\n\n"
                                 "| Load | Watts |\n|---|---|\n| CPAP | 40 |\n")
    add = a.addendum()
    for rid in ("electrical.backfeed", "electrical.dryer-outlet-hookup", "medical.medical-runtime-claim",
                "electrical.licensed-electrician-notice"):
        assert rid in add
    assert "Why:" in add and "Fix:" in add and "**Safety:**" in add
    assert add.index("MUST FIX") < len(add)
    assert a.failure_reason().startswith("CONTENT POLICY: ")
    d = a.to_dict()
    assert d["passes"] is False and d["rejects"] and d["rule_sets"] == ALL_SETS


def test_warnings_do_not_fail(pol):
    a = cp.check_markdown(pol, "The panel kit adds 240 V split-phase output.")
    assert a.passes and {v.rule_id for v in a.warnings} == {"electrical-year1.240v-mention"}
    assert "review" in a.addendum()


# ── corpora: the rule sets do not fire on the existing sites' articles ──────

def test_rule_sets_are_quiet_on_real_corpora(pol):
    """Precision check on real article bodies from the two existing sites
    (food and PC hardware): with every rule set forced on, no violation.
    (The same check over all 514 committed bodies found none either.)"""
    n = 0
    for f in sorted((FIX / "link_corpus").glob("corpus-*.json")):
        for art in json.loads(f.read_text())["articles"]:
            n += 1
            a = cp.check_markdown(pol, art["body_md"])
            assert a.violations == [], (art["file"], [(v.rule_id, v.excerpt) for v in a.violations])
    assert n >= 10


def test_policy_objects_are_immutable_snapshots(repo_cfg):
    cfg = copy.deepcopy(repo_cfg)
    p = cp.resolve_policy(rule_sets=["electrical"], config=cfg)
    cfg["rule_sets"]["electrical"]["rules"] = []
    assert p.rules                      # compiled at resolve time


# ── review round 1: the engine holds no niche vocabulary ────────────────────

def test_the_engine_default_lexicon_is_generic_english():
    """Niche words live in config (the electrical rule set's own negation
    block), so a site that does not use the set never gets them."""
    lx = cp._DEFAULT_LEXICON
    assert not lx.hazard.search("can electrocute a line worker")
    assert not lx.hazard.search("can energize every circuit")
    assert lx.hazard.search("is illegal") and lx.hazard.search("can kill a line worker")
    assert not lx.imperative_start.match("backfeed through the outlet")
    assert lx.imperative_start.match("Power stations are great")          # no noun exclusions by default
    assert not re.search(cp.DEFAULT_CALLOUTS["markdown_label_re"], "Electrical safety: x", re.I)
    defaults = " ".join(p for v in cp._LEXICON_DEFAULTS.values() for p in v)
    for niche in ("electrocut", "energi", "backfeed", "splice", "solder", "station", "NEC", "code", "shock"):
        assert niche not in defaults, niche


def test_rule_sets_carry_their_vocabulary(repo_cfg):
    el = cp.resolve_policy(rule_sets=["electrical"], config=repo_cfg)
    med = cp.resolve_policy(rule_sets=["medical"], config=repo_cfg)
    assert el.lexicon.hazard.search("can electrocute a line worker")
    assert el.lexicon.imperative_start.match("backfeed through the outlet")
    assert not el.lexicon.imperative_start.match("Power stations are great")
    assert not med.lexicon.hazard.search("can electrocute a line worker")


def test_negation_vocabulary_replace_extend_and_validate(repo_cfg):
    base = cp.merge_config(repo_cfg, {"sites": {"s": {"rule_sets": ["electrical"]}}})
    # replace a default list at the top level
    p = cp.resolve_policy("s", config=cp.merge_config(base, {"negation": {"hazard_harms": ["zap\\w*"]}}))
    assert p.lexicon.hazard.search("can zap you") and not p.lexicon.hazard.search("can injure you")
    assert p.lexicon.hazard.search("can electrocute you")              # the rule set still adds its own
    # widen the governing window
    s = "Never plug a station into the wall to backfeed the house."
    assert not cp.check_markdown(cp.resolve_policy("s", config=base), s).passes
    p6 = cp.resolve_policy("s", config=cp.merge_config(base, {"negation": {"max_gap_words": 8}}))
    assert cp.check_markdown(p6, "Never ever, under any circumstances, do backfeed.").passes
    for bad in ({"negation": {"hazard_states": ["(bad"]}}, {"negation": {"vocabulary": []}},
                {"negation": {"max_gap_words": 99}},
                {"rule_sets": {"electrical": {**repo_cfg["rule_sets"]["electrical"],
                                              "negation": {"hazard_states": ["x"]}}}}):
        with pytest.raises(cp.ContentPolicyConfigError):
            cp.resolve_policy("s", config=cp.merge_config(base, bad))


# ── review round 1: storage errors fail closed ──────────────────────────────

class _Store(LocalFilesystemStorage):
    def __init__(self, root, *, read_error=None, raw=None, listed=None):
        super().__init__(str(root))
        self.read_error, self.raw, self.listed = read_error, raw, listed

    def read_bytes(self, key):
        if self.read_error:
            raise self.read_error
        return self.raw if self.raw is not None else super().read_bytes(key)

    def list_prefix(self, prefix, limit=10000):
        return self.listed if self.listed is not None else super().list_prefix(prefix, limit)


def test_storage_copy_errors_are_config_errors(tmp_path):
    assert cp.read_storage_copy(_Store(tmp_path / "empty")) is None          # no copy: repo default
    with pytest.raises(cp.ContentPolicyConfigError, match="unreadable"):
        cp.load_config(_Store(tmp_path / "a", read_error=OSError("network down")))
    with pytest.raises(cp.ContentPolicyConfigError, match="listed but unreadable"):
        cp.load_config(_Store(tmp_path / "b", listed=[cp.CONFIG_KEY]))     # a backend that answers None
    with pytest.raises(cp.ContentPolicyConfigError, match="not valid JSON"):
        cp.load_config(_Store(tmp_path / "c", raw=b"{not json"))
    with pytest.raises(cp.ContentPolicyConfigError, match="JSON object"):
        cp.load_config(_Store(tmp_path / "d", raw=b"[1, 2]"))
    with pytest.raises(cp.ContentPolicyConfigError):
        cp.resolve_policy("powersite", storage=_Store(tmp_path / "e", read_error=TimeoutError("t")))


# ── review round 1: `unless` binds to the match ─────────────────────────────

@pytest.mark.parametrize("text, exempt", [
    ("Have a licensed electrician install a transfer switch.", True),
    ("A licensed electrician should install the interlock kit.", True),
    ("Hire a licensed electrician to wire the generator inlet box.", True),
    ("Connecting a station to it takes a transfer switch or interlock installed by a licensed electrician.", True),
    ("Never install a transfer switch without a licensed electrician.", True),
    ("Install the transfer switch; a licensed electrician should inspect it.", False),
    ("Wire the transfer switch into your main panel, or hire a licensed electrician if you're unsure.", False),
    ("Install a transfer switch, though a licensed electrician is the safer choice.", False),
    ("Don't have a licensed electrician install the transfer switch; do it yourself.", False),
    ("Install the transfer switch instead of having it installed by a licensed electrician.", False),
])
def test_unless_exempts_only_constructions_bound_to_the_match(pol, text, exempt):
    got = {v.rule_id for v in cp.check_markdown(pol, text).rejects}
    assert ("electrical.diy-transfer-wiring" not in got) is exempt, got


# ── review round 1: list lead-ins ───────────────────────────────────────────

@pytest.mark.parametrize("kind, text, negates", [
    ("heading", "What not to do", True),
    ("heading", "Common mistakes", True),
    ("heading", "Don'ts", True),
    ("paragraph", "Never do any of these during an outage:", True),
    ("paragraph", "Avoid the following:", True),
    ("heading", "Never lose power again", False),
    ("heading", "Don't get left in the dark", False),
    ("heading", "Avoid the cold: whole-home hookup steps", False),
    ("paragraph", "Dangerous storms are coming, so here is the plan:", False),
    ("paragraph", "Never do any of these during an outage.", False),         # not a lead-in without ':'
    ("heading", "Never do these instead of the steps below", False),
])
def test_lead_in_prohibits(kind, text, negates):
    assert cp.lead_in_prohibits(cp.Block("body", kind, text)) is negates


def test_a_lead_in_never_exempts_numbered_steps():
    md = "## What not to do\n\n1. Backfeed through a dryer outlet.\n\n- Backfeed through a dryer outlet.\n"
    blocks = cp.markdown_blocks(md)
    assert [(b.kind, b.negated_lead) for b in blocks] == [("heading", False), ("list_item", False),
                                                          ("list_item", False)]
    blocks = cp.markdown_blocks("## What not to do\n\n- Backfeed through a dryer outlet.\n- Use a double-male cord.\n")
    assert [b.negated_lead for b in blocks[1:]] == [True, True]


# ── review round 1: GFM tables as the kit renders them ──────────────────────

def test_tables_without_outer_pipes_and_escaped_pipes():
    md = "Intro text\nLoad | Watts | Hours\n:--|--:|--:\nCPAP \\| humid | 40 | 25\nnot a row\n"
    got = [(b.kind, b.text) for b in cp.markdown_blocks(md)]
    assert got == [("paragraph", "Intro text"), ("table_row", "Load | Watts | Hours"),
                   ("table_row", "CPAP | humid | 40 Watts | 25 Hours"), ("paragraph", "not a row")]
    assert cp.split_table_row("| a \\| b | c |") == ["a | b", "c"]
    assert cp.split_table_row("a | b") == ["a", "b"]
    # a pipe line with no delimiter row under it is not a table
    assert [b.kind for b in cp.markdown_blocks("a | b\nc | d\n")] == ["paragraph"]


def test_list_items_keep_lazy_continuation_lines():
    blocks = cp.markdown_blocks("- first item\ncontinues here\n- second\n\nAfter.\n")
    assert [(b.kind, b.text) for b in blocks] == [("list_item", "first item continues here"),
                                                  ("list_item", "second"), ("paragraph", "After.")]


# ── review round 1: the addendum names every sentence ───────────────────────

def test_addendum_lists_every_violating_sentence_up_to_the_cap(pol):
    body = "\n\n".join(f"Step {i}: backfeed through the dryer outlet number {i}." for i in range(1, 31))
    add = cp.check_markdown(pol, body).addendum()
    assert all(f"outlet number {i}." in add for i in range(1, 26))
    assert "outlet number 26." not in add and "and 5 more sentence(s)" in add
    assert "without / until / unless / if" in add                           # the lift guidance
    assert cp.ADDENDUM_MAX_EXAMPLES == 25


def test_skip_marker_in_authored_copy_is_rejected(pol):
    a = cp.check_markdown(pol, "Fine text.\n\n<span data-content-policy='skip'>Backfeed it.</span>\n")
    assert cp.SKIP_MARKER_RULE in {v.rule_id for v in a.rejects}
    off = cp.resolve_policy("no-such-site", config=cp.load_config())
    assert cp.check_markdown(off, "<span data-content-policy='skip'>x</span>").passes


def test_claim_rules_need_claims(repo_cfg):
    bad = cp.merge_config(repo_cfg, {"sites": {"s": {"rule_sets": ["x"]}}, "rule_sets": {"x": {"rules": [
        {"id": "r", "patterns": ["cpap"], "negation_target": "claim", "why": "w", "fix": "f"}]}}})
    with pytest.raises(cp.ContentPolicyConfigError, match="needs require_any"):
        cp.resolve_policy("s", config=bad)
    with pytest.raises(cp.ContentPolicyConfigError, match="negation_target"):
        cp.resolve_policy("s", config=cp.merge_config(bad, {"rule_sets": {"x": {"rules": [
            {"id": "r", "patterns": ["cpap"], "negation_target": "subject", "require_any": ["\\d"]}]}}}))


# ── review round 3: the title heads the article (gate == rendered page) ─────

def test_the_title_is_a_level_one_heading_over_the_whole_article():
    blocks = cp.article_blocks(title="Best Station for a CPAP", subtitle="Sub.", excerpt="Ex.",
                               body_md="Intro.\n\n## Pick\n\nText.\n", faqs=[{"question": "Q?", "answer": "A."}])
    assert (blocks[0].field, blocks[0].kind, blocks[0].level, blocks[0].text) == ("title", "heading", 1,
                                                                                   "Best Station for a CPAP")
    assert all(b.ancestors[:1] == (0,) for b in blocks[1:]), [(b.text, b.ancestors) for b in blocks]
    idx = {b.text: i for i, b in enumerate(blocks)}
    assert blocks[idx["Text."]].ancestors == (0, idx["Pick"])
    assert blocks[idx["A."]].ancestors == (0, idx["Q?"])
    assert [b.ancestors for b in cp.article_blocks(body_md="Intro.\n\n## Pick\n\nText.\n")] == [(), (), (1,)]


def test_a_title_naming_a_medical_device_puts_the_whole_article_in_scope(pol):
    body = "## Our pick\n\nThe Delta 2 lasts about 30 hours with the humidifier off.\n"
    a = cp.check_article(pol, title="Best Power Station for a CPAP Machine", body_md=body)
    assert _rejects(a) == ["medical.medical-runtime-claim"] and "the whole article" in a.addendum()
    assert cp.check_article(pol, title="Best Power Station for a Chest Freezer", body_md=body).passes
    # the same body under a body H1 that names the device was already a claim
    assert not cp.check_markdown(pol, "# CPAP backup\n\n" + body).passes


def test_a_title_is_never_a_list_lead_in():
    assert cp.lead_in_prohibits(cp.Block("body", "heading", "What not to do", 0, 2))
    assert not cp.lead_in_prohibits(cp.Block("title", "heading", "What not to do", 0, 1))


# ── review round 3: concessions after a warning ─────────────────────────────

@pytest.mark.parametrize("sentence, concedes", [
    ("If you absolutely must, turn off the main breaker first.", True),
    ("If you have to, keep the big door wide open.", True),
    ("If you do, plug the generator end in last.", True),
    ("If you must run it in the garage, keep the door open.", True),
    ("But if you must, flip the main off first.", True),
    ("Still, with the main off, it works in a pinch.", True),
    ("In a pinch, that's fine.", True),
    ("At the very least, turn off the main breaker.", True),
    ("If you need whole-home power, have a licensed electrician install a transfer switch.", False),
    ("If you have to evacuate, take the station with you.", False),       # a fresh condition, not a concession
    ("If you do, you could electrocute a line worker.", False),           # no instruction, no permission
    ("If you do, never touch the exposed prongs.", False),                # a prohibition
    ("If you must run one, keep it outdoors and 20 feet from the house.", False),   # the safe alternative
    ("That said, it's fine to use a power station indoors.", False),      # a dummy "it" with its own infinitive
    ("Still, a transfer switch works well.", False),                      # another noun works
    ("Keep it outdoors, at least 20 feet from doors and windows.", False),          # no opener
])
def test_concedes(pol, sentence, concedes):
    assert cp.concedes(sentence, pol.lexicon) is concedes


def test_concedes_counts_the_steps_a_colon_introduces(pol):
    lead = "Here's how to do it as safely as possible:"
    assert not cp.concedes(lead, pol.lexicon)
    assert cp.concedes(lead, pol.lexicon, ["Shut off the main breaker."])
    assert not cp.concedes(lead, pol.lexicon, ["Have a licensed electrician install a transfer switch."])


def test_a_concession_violation_names_the_warning_and_the_follow_up(pol):
    a = cp.check_markdown(pol, "Intro line.\n\nNever bypass the BMS. If you must, use a jumper wire across the "
                               "cold-temp sensor.\n")
    [v] = [v for v in a.rejects if v.rule_id == "electrical.bms-bypass"]
    assert v.excerpt == "Never bypass the BMS. If you must, use a jumper wire across the cold-temp sensor."
    assert (v.matched, v.field, v.line, v.kind) == ("If you must", "body", 3, "pattern")
    assert "concession" in a.addendum() and "If you must" in a.addendum()


def test_the_concession_window_is_bounded_and_ends_at_a_heading(pol):
    warn = "Never backfeed through a dryer outlet. "
    late = "It is illegal. It can kill line workers. Still, with the main off, it works."
    assert cp.check_markdown(pol, warn + late).passes                               # 3 sentences on: out of range
    assert not cp.check_markdown(pol, warn + late.split(" ", 3)[3]).passes          # 2 sentences on
    assert cp.check_markdown(pol, warn + "\n\n## Next\n\nStill, with the main off, it works.").passes
    assert not cp.check_markdown(pol, warn + "\n\n## Still, it works\n\nTurn off the main first.\n").passes


def test_concession_settings_are_config_and_validated(repo_cfg):
    base = cp.merge_config(repo_cfg, {"sites": {"s": {"rule_sets": ["electrical"]}}})
    text = "Never backfeed. If you must, turn off the main breaker first."
    assert not cp.check_markdown(cp.resolve_policy("s", config=base), text).passes
    off = cp.resolve_policy("s", config=cp.merge_config(base, {"negation": {"concession_window": 0}}))
    assert cp.check_markdown(off, text).passes
    rules = [{**r, "concession": False} for r in repo_cfg["rule_sets"]["electrical"]["rules"]]
    opted_out = cp.merge_config(base, {"rule_sets": {"electrical": {**repo_cfg["rule_sets"]["electrical"],
                                                                    "rules": rules}}})
    assert cp.check_markdown(cp.resolve_policy("s", config=opted_out), text).passes
    shove = "Never backfeed. If push comes to shove, turn off the main breaker first."
    assert cp.check_markdown(cp.resolve_policy("s", config=base), shove).passes
    ext = cp.merge_config(base, {"negation": {"extra_concession_openers": [r"if\s+push\s+comes\s+to\s+shove\b"]}})
    assert not cp.check_markdown(cp.resolve_policy("s", config=ext), shove).passes
    for bad in ({"negation": {"concession_window": 9}}, {"negation": {"concession_window": "x"}},
                {"negation": {"extra_concession_openers": ["(bad"]}}):
        with pytest.raises(cp.ContentPolicyConfigError):
            cp.resolve_policy("s", config=cp.merge_config(base, bad))
    rule = {"id": "r", "patterns": ["x"], "why": "w", "fix": "f"}
    for extra, msg in (({"concession_subjects": ["gen"]}, "needs concession_claims"),
                       ({"concession": "yes"}, "concession"), ({"concession_claims": ["(bad"]}, "invalid regex")):
        cfg = cp.merge_config(base, {"sites": {"s": {"rule_sets": ["x"]}},
                                     "rule_sets": {"x": {"rules": [{**rule, **extra}]}}})
        with pytest.raises(cp.ContentPolicyConfigError, match=msg):
            cp.resolve_policy("s", config=cfg)


def test_claim_rules_carry_instead_of_using_concessions(pol):
    assert {r.id for r in pol.rules if r.concession} == {r.id for r in pol.rules if r.negation_target == "anchor"}
    # a claim rule's subject already carries through the paragraph
    a = cp.check_markdown(pol, "We don't publish CPAP runtimes. If you must know, a 1,000 Wh station gives about "
                               "two nights.")
    assert _rejects(a) == ["medical.medical-runtime-claim"]


def test_each_coordinated_instruction_is_judged_on_its_own(pol):
    """A safe alternative in one instruction no longer launders a harmful one
    coordinated with it, so the hazard statement is not an exemption."""
    lx = pol.lexicon
    s1 = "Backfeeding can kill line workers, so hire a licensed electrician or just turn off the main first."
    s2 = ("Backfeeding can kill line workers, so hire a licensed electrician to install a transfer switch or "
          "interlock kit.")
    assert cp.negation_reason(s1, 0, len("Backfeeding"), lx) == ""
    assert cp.negation_reason(s2, 0, len("Backfeeding"), lx) == "hazard"


def test_claim_vocabulary_stays_linear_on_long_runs_of_number_words(pol):
    """The spelled-out number vocabulary bounds its repetition: a run of
    thousands of number words used to take seconds (quadratic backtracking)
    and could stall the publish gate."""
    import time

    text = "A CPAP " + " ".join(["twenty-one and thirty"] * 1500) + " things."
    t = time.perf_counter()
    assert cp.check_markdown(pol, text).passes
    assert time.perf_counter() - t < 2.0
