"""framework/cli/article_link_guard.py `check` — the pre-INSERT self-check the
article author runs (ARTICLE_AUTHOR.md). The Opus author INSERTs its own rows,
so the wrapper's INSERT-time guard never saw the seven guides that shipped
with zero buy links (2026-11-12 review); this is the same contract, run by the
author before its INSERT. No network, no DB: repo default config only."""
from __future__ import annotations

import json

from framework.cli import article_link_guard as cli

PRODUCT_PAGES_ONLY = (
    "# Gifts for Bakers 2026\n\n"
    + " ".join(f"Try [recipe {i}](/recipes/cookie-{i}-{100 + i})." for i in range(6))
    + " Our pick is the [OXO scale](/kitchen/oxo-good-grips-11-pound-scale) and the "
      "[Nordic Ware sheet](/kitchen/nordic-ware-naturals-half-sheet).\n"
)


def _run_dir(tmp_path, proposal, agent_id="aisleprompt-article-proposal-agent"):
    (tmp_path / "recommendations.json").write_text(json.dumps({"recommendations": [
        {"id": "art-001", "agent_id": agent_id, "proposal": proposal}]}))
    return tmp_path


def test_check_fails_a_gift_guide_without_buy_links_and_prints_the_fix(tmp_path, capsys):
    prop = {"slug": "gifts-for-bakers", "title": "Gifts for Bakers 2026", "bucket": "seasonal-occasion",
            "expected_kitchen_product_slugs": ["oxo-good-grips-11-pound-scale",
                                               "nordic-ware-naturals-half-sheet"]}
    rd = _run_dir(tmp_path, prop)
    body = tmp_path / "gifts-for-bakers.body.md"
    body.write_text(PRODUCT_PAGES_ONLY)
    rc = cli.main(["check", "--run-dir", str(rd), "--rec-id", "art-001", "--body", str(body),
                   "--no-storage"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("FAIL only 0 direct buy links (min 2)")
    assert "0/2 required direct buy links" in out
    assert "DIRECT BUY LINKS — at least 2 distinct" in out
    assert "/k/oxo-good-grips-11-pound-scale?source=amazon" in out

    body.write_text(PRODUCT_PAGES_ONLY
                    + "[Check price](https://aisleprompt.com/k/oxo-good-grips-11-pound-scale?source=amazon) "
                      "[Check price](/k/nordic-ware-naturals-half-sheet?source=amazon)\n")
    rc = cli.main(["check", "--run-dir", str(rd), "--rec-id", "art-001", "--body", str(body),
                   "--no-storage"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("PASS ")


def test_check_without_a_monetized_bucket_or_title_needs_no_buy_link(tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text(PRODUCT_PAGES_ONLY)
    rc = cli.main(["check", "--site", "aisleprompt", "--bucket", "recipe-cluster",
                   "--title", "Diwali Recipes: 20 Sweets", "--body", str(body), "--no-storage"])
    assert rc == 0, capsys.readouterr().out


def test_check_bad_input_is_exit_2(tmp_path, capsys):
    rd = _run_dir(tmp_path, {"slug": "x"})
    assert cli.main(["check", "--run-dir", str(rd), "--rec-id", "art-404",
                     "--body", str(tmp_path / "missing.md"), "--no-storage"]) == 2
    assert cli.main(["check", "--site", "aisleprompt", "--body", str(tmp_path / "missing.md"),
                     "--no-storage"]) == 2
    assert "cannot read" in capsys.readouterr().err
