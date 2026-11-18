"""Amazon catalog-discovery engine (agents/amazon-catalog-discovery-agent).

A thin, niche-agnostic layer around ``framework.core.amazon_discovery``:

* :mod:`.config`   -- the per-site config model and its validator
* :mod:`.lanes`    -- title / browse-node lane classifier (our own labels)
* :mod:`.rotation` -- which (query, itemPage) slots a run searches, resumed
                     across runs, with the paging rules the shared loop lacks
* :mod:`.seeds`    -- seed-ASIN import from JSON files and read-only SQL
* :mod:`.catalog`  -- the site's catalog table: dedupe and ASIN-only inserts
* :mod:`.engine`   -- one run: seeds, then searches, then metrics

Nothing here names a site or a niche; every value comes from the site's
config. Nothing here persists Amazon content: an inserted row carries the
ASIN and our own lane label, confidence and provenance only.
"""

__all__ = ["config", "lanes", "rotation", "seeds", "catalog", "engine"]
