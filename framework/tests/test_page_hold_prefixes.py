"""page_hold additions for growth dispatch (2026-11-11): prefix* targets,
holds read from every agent yaml in a site repo, the brief's held-pages line.
"""
from datetime import date

from framework.core import page_hold as ph

HELD = "/blog/best-pressure-cookers-2026"
TODAY = date(2026, 11, 11)


def _holds(until="2026-12-02"):
    return ph.holds_from_site_config({"page_holds": [
        {"path": HELD, "reason": "picks need a person", "until": until}]})


def test_prefix_targets_cover_held_pages():
    holds = _holds()
    for targets in (["/blog/best-*"], ["https://site.test/blog/*"], "/blog/best-*",
                    ["*"], ["/BLOG/BEST-*"]):
        assert ph.rec_holds({"target_urls": targets}, holds, today=TODAY), targets
    assert ph.rec_holds({"pages": ["/blog/*"]}, holds, today=TODAY)
    for targets in (["/recipes/*"], ["/blog/best-air-fryers-*"], ["/blog/best-air-fryers-2026"]):
        assert not ph.rec_holds({"target_urls": targets}, holds, today=TODAY), targets
    # a "/*" in free text (a CSS comment in a code snippet) is not a prefix
    assert not ph.rec_holds({"description": "body { color: red; } /* fix */",
                             "target_urls": ["/recipes/a"]}, holds, today=TODAY)
    assert ph.rec_prefixes({"target_urls": ["https://site.test/blog/best-*", "/x"],
                            "url": "*"}) == ["/blog/best-", "/"]
    assert ph.rec_prefixes({"target_urls": 5}) == []
    # override and expiry still apply to prefixes
    assert not ph.rec_holds({"target_urls": ["/blog/*"], "hold_override": True}, holds, today=TODAY)
    assert not ph.rec_holds({"target_urls": ["/blog/*"]}, holds, today=date(2026, 12, 3))


def test_holds_from_repo_unions_every_agent_yaml(tmp_path):
    agents = tmp_path / "agents"
    for name, body in {
        "a": f"page_holds:\n  - path: {HELD}\n    reason: needs a person\n    until: '2026-12-02'\n",
        "b": f"implementer:\n  page_holds:\n    - path: https://site.test{HELD}/\n      until: '2026-12-10'\n",
        "c": "site: {id: x}\n",
        "d": "page_holds: [unclosed\n",
        "e": "page_holds:\n  - /recipes/held-recipe-page\n",
    }.items():
        (agents / name).mkdir(parents=True)
        (agents / name / "site.yaml").write_text(body)
    holds = {h.path: h for h in ph.holds_from_repo(tmp_path)}
    assert set(holds) == {HELD, "/recipes/held-recipe-page"}
    assert holds[HELD].until == date(2026, 12, 10)            # the longer hold wins
    assert ph.holds_from_repo(tmp_path / "missing") == [] and ph.holds_from_repo("") == []
    assert ph.holds_from_repo(tmp_path, patterns=("agents/c/site.yaml",)) == []
    assert [h.path for h in ph.holds_from_repo(tmp_path, "agents/e/site.yaml")] == \
        ["/recipes/held-recipe-page"]                         # a bare glob string


def test_holds_prompt_line_and_round_trip():
    holds = _holds()
    line = ph.holds_prompt_line(holds, today=TODAY)
    assert line.startswith("HELD PAGES") and f"{HELD} (picks need a person; until 2026-12-02)" in line
    assert "prefix*" in line and "dropped in code" in line
    assert ph.holds_prompt_line(holds, today=date(2026, 12, 3)) == ""
    back = ph.holds_from_site_config({"page_holds": [h.to_dict() for h in holds]})
    assert back == holds
