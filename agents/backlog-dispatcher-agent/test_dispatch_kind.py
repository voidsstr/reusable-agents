"""The dispatch-kind allowlist must classify recs the way the implementer does.

2026-10-09: the producer id `*-article-proposal-agent` fell through to
"general", so restricting the implementer to authoring work (the documented
pause) would also have held back every blog post.

    python3 -m pytest agents/backlog-dispatcher-agent/test_dispatch_kind.py
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "backlog_dispatcher_agent", pathlib.Path(__file__).with_name("agent.py"))
disp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disp)

AUTHORING = ["article-author", "article-refresh", "catalog-audit", "h2h"]


def test_article_proposal_producer_runs_as_article_author():
    rec = {"id": "art-001", "type": "article-author-proposal"}
    assert disp._dispatch_kind_for("aisleprompt-article-proposal-agent", rec) == "article-author"
    assert disp._dispatch_kind_for("specpicks-article-proposal-agent", {"id": "art-001"}) == "article-author"


def test_article_author_rec_type_alone_is_enough():
    assert disp._dispatch_kind_for("some-producer", {"type": "article-author-proposal"}) == "article-author"


def test_code_change_producers_stay_out_of_an_authoring_allowlist():
    for producer in ("specpicks-seo-opportunity-agent", "aisleprompt-progressive-improvement-agent",
                     "aisleprompt-user-growth-strategist", "specpicks-shelf-audit-agent"):
        assert disp._dispatch_kind_for(producer, {"id": "rec-001"}) not in AUTHORING


def test_db_only_producers_keep_their_kinds():
    assert disp._dispatch_kind_for("specpicks-head-to-head-agent", {}) == "h2h"
    assert disp._dispatch_kind_for("aisleprompt-catalog-audit-agent", {}) == "catalog-audit"


def test_rec_dispatch_kind_override_wins():
    rec = {"dispatch_kind": "article-refresh"}
    assert disp._dispatch_kind_for("aisleprompt-progressive-improvement-agent", rec) == "article-refresh"
