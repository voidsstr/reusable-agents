# affiliate-article-agent

A generic AgentBase engine that writes one data-backed Opus guide per run for
an affiliate site whose pages render from its own tables, and hands it to
`framework.cli.article_publish_gate` as a pending row. See `AGENT.md` for the
phases, the dry run and the failure modes, and `site.example.yaml` for every
config key.

Why a new engine instead of the implementer's article-author path: the topic
is chosen in code from the site's calendar (no ranking call, no untrusted text
in the prompt), every number comes from the site's SQL and is checked against
it after the model answers, the prose is one strict-JSON call, and the row
can only reach the site through the publish gate. It is not named
`*-article-proposal-agent`, so the backlog dispatcher never sweeps it.

```bash
python3 agents/affiliate-article-agent/agent.py --config --site-yaml <instance site.yaml>
python3 agents/affiliate-article-agent/agent.py --dry-run --no-storage --site-yaml <...> --out /tmp/aae
python3 -m pytest framework/tests/test_affiliate_article_agent.py framework/tests/test_affiliate_article_red_team.py -q
```

New framework primitives (new files; nothing shared was edited):

| Module | Role |
|---|---|
| `framework/core/authoring_capacity.py` | blackout windows, not-before dates, claude-pool readiness vs `CLAUDE_POOL_RESERVE`, recent production-author defers (defer-backoff and run-dir `deferred.json`, read-only, fail closed), the day's model-usage ledger |
| `framework/core/editorial_calendar.py` | calendar file, readiness requirements, the next ready topic |
| `framework/core/facts_pack.py` | SQL facts pack, read-only, asserted free of prices / offers / Amazon text; numeric index for the claims guard |
| `framework/core/guide_claims_guard.py` | numbers vs facts within rounding and scoped to the model they are about, verdict / status / context row rules, 6(y) and money wording, guarantees, retail events, topic bans, Amazon / off-site links, raw HTML, images, hand-written tables, placeholder grammar |
