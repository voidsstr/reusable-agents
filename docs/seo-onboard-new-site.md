# Onboarding a new site to the SEO agent

The reusable-agents SEO stack works for any site through a single
`site.yaml` config. There are no hardcoded site values anywhere in the
analyzer, collector, reporter, implementer, or deployer code — adding a
new site is **configuration only**.

## What you get

For each site you onboard, the SEO agent runs every 3 hours and:

1. **Crawls the sitemap** + samples pages by type (product / head-to-head
   / article / buying-guide / hardware).
2. **Pulls Google Search Console + GA4 data** for the last 90 days
   (impressions, clicks, CTR, position, country breakdown).
3. **Scans every published article** when `articles.audit_full_body=true`
   — including body_md so the analyzer can flag any product mention
   missing a tagged Amazon affiliate link (revenue-focus mode).
4. **Audits all sampled HTML** for SEO basics (title, meta description,
   canonical, JSON-LD completeness, internal-link density, citation
   count, Amazon outbound-tag coverage).
5. **Emits ranked recommendations** (`recommendations.json`) that go
   through the analyzer's rule passes:
   - Striking-distance (positions 6-20 with high impressions)
   - Zero-click (high impressions, zero clicks → snippet rewrite)
   - Coverage gaps (new-page recs against your `coverage_targets`)
   - Pre-traffic content engine (when GSC has < 100 impressions/90d)
   - Schema completeness, on-page, pros/cons density, body-link density
   - Affiliate-tag leak (Amazon links missing `?tag=`)
   - Article × featured-product mention attribution audit
6. **Auto-queues every rec** for the implementer (no email reply needed).
   The reporter still sends an informational email; replies are only used
   to defer / skip / revert.
7. **The implementer** drives a Claude Code session against `AGENT.md`
   to apply the proposed edits, runs tests, commits + tags, and
   optionally chains to a deployer.

## 5-step onboarding

### 1. Copy the generic site config

```bash
cd /home/voidsstr/development/reusable-agents
cp examples/sites/generic.yaml examples/sites/<your-site>.yaml
```

Edit it. The mandatory blocks are `site`, `data_sources.gsc`,
`data_sources.ga4`, `analyzer`, `reporter.email`, `auth`. Everything
else is optional but adds capability — see the comments in
`generic.yaml` for what each block enables.

For an Amazon-affiliate site that wants the article-mention audit:

```yaml
revenue_focus:
  enabled: true
  amazon_associate_tag: "yourassoc-20"
  product_url_template: "https://yoursite.com/products/{asin}"
  article_renderer_files:
    - frontend/src/pages/ArticleDetailPage.tsx

articles:
  audit_full_body: true
  audit_all: true
  sources:
    - name: editorial_articles
      url_template: "https://{domain}/reviews/{slug}"
      query: |
        SELECT slug, title, body_md AS body, ...
        FROM editorial_articles
        WHERE site_id='your-site' AND status='published'
```

### 2. Drop OAuth credentials

The collector needs Google OAuth tokens for GSC + GA4. The reporter
reuses them for Microsoft Graph (sending the email via
`automation@northernsoftwareconsulting.com`).

```bash
# Per-site OAuth file — path matches cfg.auth.oauth_file
mkdir -p ~/.reusable-agents/seo
# Either bootstrap a fresh token (browser flow):
python3 agents/seo-data-collector/refresh-token.py
# ...or re-use an existing one and add the site to the GSC + GA4 properties.
```

### 3. Add per-site env vars

For sites with a backend DB, the implementer resolves `DATABASE_URL`
from `DATABASE_URL_<UPPER_SITE>` (with `-` → `_`). Add it to
`~/.reusable-agents/secrets.env`:

```bash
# ~/.reusable-agents/secrets.env (mode 0600)
DATABASE_URL_YOURSITE=postgresql://user:pass@host/db?sslmode=require
```

For Amazon-affiliate sites also add the BrightData key (used by the
product-hydration-agent's price refresh):

```bash
BRIGHTDATA_API_KEY=...
```

### 4. Create a per-site agent manifest

Copy an existing per-site SEO agent dir and adjust:

```bash
cp -r /home/voidsstr/development/specpicks/agents/seo-opportunity-agent \
      /home/voidsstr/development/<your-site-repo>/agents/seo-opportunity-agent
cd /home/voidsstr/development/<your-site-repo>/agents/seo-opportunity-agent
```

Edit `manifest.json`:
- `id`: `<your-site>-seo-opportunity-agent`
- `cron_expr`: stagger off the existing every-3h schedules
  (e.g. `15 */3 * * *` if specpicks holds `30 */3 * * *`)
- `entry_command`: point `SEO_AGENT_CONFIG` at your new site.yaml
- `metadata.site`: `your-site`
- `confirmation_flow.kind`: `auto-queue-with-notification` (keeps the
  no-reply-needed default)

Edit `site.yaml` to match what you put at
`reusable-agents/examples/sites/<your-site>.yaml` (or just symlink it).

### 5. Register

```bash
FRAMEWORK_API_URL=http://localhost:8093 \
    FRAMEWORK_API_TOKEN=<token> \
    bash /home/voidsstr/development/reusable-agents/install/register-all-from-dir.sh \
    /home/voidsstr/development/<your-site-repo>/agents

# Refresh the systemd timers
python3 /home/voidsstr/development/reusable-agents/install/write-systemd-timers.py
```

The framework auto-creates a systemd `--user` timer for the new agent.
Verify with:

```bash
systemctl --user list-timers | grep your-site
curl -s -H "Authorization: Bearer <token>" \
    http://localhost:8093/api/agents/your-site-seo-opportunity-agent
```

## What the agent does on every run

```
0:00  collector starts
0:30  collector finishes (GSC + GA4 + DB queries + sitemap crawl + page sample)
0:35  analyzer starts — runs 12+ rule passes
1:30  analyzer finishes — writes recommendations.json
1:31  reporter sends informational email + writes auto-queue trigger
1:32  responder-agent picks up the auto-queue file (it polls every minute)
1:33  responder dispatches to implementer (in batches of 12 if more recs)
1:34  implementer runs Claude Code against AGENT.md applying the edits
       — typical: 20-90 minutes depending on rec complexity + count
2:30+  implementer commits, optionally tags + deploys, sends per-rec
       completion emails (which the digest-rollup-agent stitches into
       the next 3h summary).
```

You'll see a single rolled-up email from the digest-rollup-agent every
3 hours summarizing all SEO + article-author + h2h + hydration agent
activity across all sites. No per-rec replies needed.

## Reference implementations

- **specpicks** (`examples/sites/specpicks.yaml`) — full revenue-focus
  config with Amazon Associates, article-mention audit, featured-product
  curation, BrightData price refresh.
- **aisleprompt** (`examples/sites/aisleprompt.yaml`) — content-focused
  site without affiliate revenue, recommend mode.

## Anti-patterns (don't do these)

1. ❌ **Don't fork analyzer.py to add per-site rules.** If a rule is
   site-specific, add a config knob in site.yaml that gates it.
2. ❌ **Don't hardcode connection strings or affiliate tags anywhere.**
   Both have happened in the past. The implementer's `run.sh` and the
   analyzer's article-mention pass are both fully config-driven now —
   keep it that way.
3. ❌ **Don't add a separate per-site responder route.** Auto-queue is
   the universal path. Replies (defer / skip / revert) are an override,
   not the trigger.
4. ❌ **Don't skip `revenue_focus.product_url_template`** if the site
   has its own PDPs. The analyzer's mention audit will only flag
   amazon.com URLs and miss internal PDP links otherwise — meaning
   articles that already link to your own PDP get a false-positive
   "untagged" rec.
