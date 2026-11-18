---
name: amazon-catalog-discovery-agent
description: Engine that grows an affiliate site's catalog from Amazon Creators searchItems over the site's own lanes, persisting only the ASIN and our lane label.
---

You are working on the **Amazon Catalog Discovery** engine. Read
[`AGENT.md`](AGENT.md) first: it documents the run phases, the lane rules,
the paging rules that work around the shared `discover()` defects (W3), the
budget, the seed sources, the goals and the failure modes.

Rules that matter when you change it:

- Niche and site values (lanes, seeds, browse nodes, price bands, reject
  patterns, seed sources) live in the site's config, never in this code.
- Never persist, log or report an Amazon title, price, image, URL or brand.
  `insert_sql` may only use the parameters in `config.INSERT_PARAMS`.
- Never request an itemPage above 10 (a config may lower the ceiling, never
  raise it), never stop a query on a short page, and never end a query on a
  server error (5xx: the slot is retried by a later run).
- Never let a config lower the $1 pricing floor (`pricing.min_price`, a
  seed's `min_price`).
- A safety boundary (e.g. a site's year-1 cord exclusions) is a GLOBAL
  `reject_patterns` rule, negation-aware, never only one lane's `exclude`.
  It screens seed records that have a title too; a seed label without a
  title is imported only for lanes the source trusts (`lanes` /
  `trust_labels`).
- Seed SQL is one read-only SELECT; it runs as a keyset subquery in a
  `default_transaction_read_only=on` session.
- Never exceed the run's call allowance (`budget_overrun_calls` must stay 0).
- Run `framework/tests/test_amazon_catalog_discovery_agent.py` after any change
  (it pins the lane classifier to the sizing run's reference classifier).
