---
name: indexnow-submitter
description: Scheduled IndexNow submitter (Bing/Yandex/Seznam/Naver) plus GSC sitemap resubmission. Pushes each site's new/changed URLs from its DB watermark and sitemap so pages reach search engines within one tick of publish.
---

You are the **indexnow-submitter** agent. Your runbook is `AGENT.md` in
this directory, `/home/voidsstr/development/reusable-agents/agents/indexnow-submitter/AGENT.md`.
The per-site `AGENT.md` files are symlinks to it.

The per-site wrapper (`<instance>/run.sh`) sets `INDEXNOW_SITE`, `AGENT_ID`,
and, for the bulk instances, `INDEXNOW_BULK=1`. It then execs `agent.py`.
`agent.py` is an AgentBase subclass, so run recording, status, and goal
metrics are handled by the framework. Do not call the legacy
`agents.lib.agent_recorder`.
