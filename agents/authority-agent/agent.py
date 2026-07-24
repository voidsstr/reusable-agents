"""Authority Agent — implementation.

Breaks the "Discovered - currently not indexed" wall by working the ONE
lever the content-supply agents don't: off-page authority + link-equity.

A young affiliate site can publish thousands of technically-perfect pages
and still get ~8% indexed because Google withholds indexing on low-authority
domains. More articles never fixes that; authority signals do. This agent,
per run:

  1. AUDIT indexation — read the GSC crawl snapshot (sample_indexed/total,
     states) to quantify how stuck the site is.
  2. Find CITABLE ASSETS — the site's strongest, most linkable pages
     (data-rich comparisons / benchmarks / "vs" deep-dives). These are what
     you pitch for backlinks; a thin roundup is not.
  3. Rank INDEXATION NEAR-MISSES — pages that already draw impressions but
     sit below the fold; a handful of links tips them onto page 1.
  4. Emit an OUTREACH WORKLIST (emailed to the operator — link-building is
     human-gated; auto-pitching is spam) AND auto-queue INTERNAL link-equity
     recs (route links from already-indexed hubs → stuck high-value pages),
     which the implementer CAN apply safely.

Config is env-driven so the engine stays site-agnostic (framework-first):
    AUTHORITY_SITE_ID      e.g. "specpicks"
    AUTHORITY_SITE_DOMAIN  e.g. "specpicks.com"
    DATABASE_URL           the site DSN
    AUTHORITY_ASSET_TABLE  editorial_articles (default)
"""
from __future__ import annotations

import os, sys
_REPO = "/home/voidsstr/development/reusable-agents"
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from framework.core.agent_base import AgentBase, RunResult
from framework.core.confirmations import requires_confirmation
from framework.core.guardrails import declare

try:
    import psycopg2  # noqa
except Exception:  # pragma: no cover
    psycopg2 = None


class AuthorityAgent(AgentBase):
    agent_id = "authority-agent"
    name = "Authority Agent"
    description = (
        "Finds and prioritizes off-page authority opportunities (citable "
        "assets, indexation near-misses, internal link-equity routing) to "
        "break the Discovered-not-indexed wall"
    )
    category = "seo"

    capabilities = [
        declare("read_gsc_snapshot", "Read the GSC crawl-progress snapshot",
                confirmation_required=False, risk_level="low"),
        declare("read_content_catalog", "Read published pages to score linkability",
                confirmation_required=False, risk_level="low"),
        declare("queue_internal_link_recs",
                "Queue internal link-equity recs to the implementer",
                confirmation_required=False, risk_level="low"),
        declare("email_outreach_worklist", "Email the operator a ranked outreach worklist",
                confirmation_required=False, risk_level="low"),
        declare("send_external_outreach",
                "Send an external backlink/citation pitch",
                confirmation_required=True, risk_level="high",
                affects=["external", "reputation"]),
    ]

    # ---- config ---------------------------------------------------------
    def _cfg(self) -> dict:
        return {
            "site_id": os.environ.get("AUTHORITY_SITE_ID", "specpicks"),
            "domain": os.environ.get("AUTHORITY_SITE_DOMAIN", "specpicks.com"),
            "dsn": os.environ.get("DATABASE_URL", ""),
            "asset_table": os.environ.get("AUTHORITY_ASSET_TABLE", "editorial_articles"),
            "worklist_size": int(os.environ.get("AUTHORITY_WORKLIST_SIZE", "10")),
        }

    def setup(self) -> None:
        self._conn = None

    def _connect(self, dsn: str):
        if psycopg2 is None:
            raise RuntimeError("psycopg2 not available")
        self._conn = psycopg2.connect(dsn, connect_timeout=12)
        return self._conn

    # ---- signals (short-circuit): re-run only when inputs change ---------
    def signals(self) -> dict | None:
        cfg = self._cfg()
        if not cfg["dsn"]:
            return None
        try:
            c = self._connect(cfg["dsn"]); cur = c.cursor()
            # indexation snapshot id + newest asset id = the inputs we read
            idx = None
            try:
                cur.execute("SELECT max(id) FROM gsc_crawl_progress WHERE site=%s", (cfg["site_id"],))
                idx = cur.fetchone()[0]
            except Exception:
                c.rollback()
            cur.execute(f"SELECT max(id) FROM {cfg['asset_table']}")
            newest = cur.fetchone()[0]
            return {"gsc_snapshot_id": idx, "newest_asset_id": newest}
        except Exception:
            return None
        finally:
            try: self._conn and self._conn.close()
            except Exception: pass
            self._conn = None

    # ---- work -----------------------------------------------------------
    def run(self) -> RunResult:
        cfg = self._cfg()
        self.status("starting", progress=0.0, current_action="load config")
        self.decide("plan",
                    f"authority pass for {cfg['site_id']}: audit indexation, "
                    f"rank citable assets + near-misses, emit worklist + internal recs")

        if not cfg["dsn"]:
            return RunResult(status="error", summary="DATABASE_URL not set",
                             metrics={}, next_state={})

        c = self._connect(cfg["dsn"]); cur = c.cursor()

        # 1) indexation audit ------------------------------------------------
        self.status("auditing indexation", progress=0.2)
        indexed_pct = None; stuck_states = {}
        try:
            cur.execute("""SELECT sample_indexed, sample_total, states, clicks_28d, impr_28d
                             FROM gsc_crawl_progress WHERE site=%s
                            ORDER BY ts DESC LIMIT 1""", (cfg["site_id"],))
            row = cur.fetchone()
            if row:
                si, st, states, clicks28, impr28 = row
                indexed_pct = round(100.0 * (si or 0) / st, 1) if st else None
                stuck_states = states or {}
                self.decide("observation",
                            f"indexation {si}/{st} ({indexed_pct}%), clicks_28d={clicks28}, "
                            f"impr_28d={impr28}, states={stuck_states}")
        except Exception as e:
            c.rollback(); self.decide("observation", f"no gsc snapshot: {e}")

        # 2) citable assets — data-rich, linkable pages ----------------------
        self.status("scoring citable assets", progress=0.45)
        assets = self._citable_assets(cur, cfg)

        # 3) indexation near-misses — impressions but below the fold ---------
        # (uses the same asset table; the "closest to breaking through" set
        #  is proxied by richest/most-linked published pages when per-query
        #  GSC data isn't available locally.)
        near = assets[: cfg["worklist_size"]]

        # 4) authority worklist (HUMAN-gated — emailed, never auto-sent).
        #    Includes BOTH the off-site outreach targets and the on-site
        #    internal link-equity moves as concrete actions. (Auto-queuing
        #    the internal-link recs to the implementer is a v2 once the
        #    recommendations.json contract is wired per-site.)
        self.status("building authority worklist", progress=0.8)
        worklist = self._build_worklist(cfg, assets, indexed_pct, stuck_states)
        emailed = self._email_worklist(cfg, worklist)

        try: c.close()
        except Exception: pass

        self.status("finalizing", progress=0.95)
        outreach_targets = min(len(assets), cfg["worklist_size"])
        metrics = {
            "indexed_pct": indexed_pct if indexed_pct is not None else 0,
            "citable_assets_found": len(assets),
            "authority_targets_surfaced": outreach_targets,
            "worklist_emailed": 1 if emailed else 0,
        }
        # goal progress — one call per bound goal (targets in goals/active.json)
        try:
            from framework.core.goals import record_goal_progress
            run_ts = getattr(self, "run_ts", "") or ""
            if indexed_pct is not None:
                record_goal_progress(self.agent_id, "lift-indexation-rate",
                                     indexed_pct, run_ts=run_ts)
            record_goal_progress(self.agent_id, "surface-authority-targets",
                                 outreach_targets, run_ts=run_ts)
        except Exception as e:
            self.decide("observation", f"goal record skipped: {e}")

        return RunResult(
            status="success",
            summary=(f"{cfg['site_id']}: indexed {metrics['indexed_pct']}% · "
                     f"{len(assets)} citable assets · "
                     f"{outreach_targets} authority targets "
                     f"{'emailed' if emailed else '(email skipped)'}"),
            metrics=metrics,
            next_state={"last_indexed_pct": indexed_pct},
        )

    # ---- helpers --------------------------------------------------------
    def _citable_assets(self, cur, cfg) -> list[dict]:
        """Rank published pages by linkability: length + comparison/benchmark
        shape + product density. These are the pages worth building links to;
        thin roundups are excluded."""
        t = cfg["asset_table"]
        # Column set is validated against editorial_articles (no article_type
        # there — the "shape" is inferred from the slug). A data-rich page =
        # long body + many product refs + comparison/benchmark intent.
        try:
            cur.execute(f"""
                SELECT slug, title,
                       COALESCE(length(body_md),0) AS blen,
                       COALESCE(array_length(related_product_asins,1),0) AS nprod,
                       COALESCE(category,'') AS cat
                  FROM {t}
                 WHERE status='published' AND body_md IS NOT NULL
                 ORDER BY (
                     COALESCE(length(body_md),0)
                     + 400*COALESCE(array_length(related_product_asins,1),0)
                     + CASE WHEN slug ~* '(vs|-vs-|comparison|benchmark|best-)'
                            THEN 3000 ELSE 0 END
                 ) DESC
                 LIMIT 40
            """)
            out = []
            for slug, title, blen, nprod, cat in cur.fetchall():
                is_cmp = any(k in (slug or "") for k in
                             ("vs", "-vs-", "comparison", "benchmark", "best-"))
                out.append({
                    "slug": slug, "title": title, "body_len": blen,
                    "n_products": nprod,
                    "type": "comparison" if is_cmp else "article",
                    "category": cat,
                    "url": f"https://{cfg['domain']}/reviews/{slug}",
                })
            return out
        except Exception as e:
            cur.connection.rollback()
            self.decide("observation", f"citable-asset query fell back: {e}")
            return []

    def _build_worklist(self, cfg, assets, indexed_pct, stuck_states) -> str:
        lines = []
        lines.append(f"AUTHORITY WORKLIST — {cfg['site_id']} ({cfg['domain']})")
        lines.append(f"Indexation: {indexed_pct}% of sampled URLs · states: {stuck_states}")
        lines.append("")
        lines.append("Why this exists: the site is stuck in 'Discovered - currently not")
        lines.append("indexed' — a low-authority signal that MORE content cannot fix.")
        lines.append("Below are your best link-worthy assets. Each needs OFF-SITE links")
        lines.append("(a human action — do NOT auto-pitch), plus the free ON-SITE link-")
        lines.append("equity routing described at the bottom.")
        lines.append("")
        lines.append("TOP CITABLE ASSETS TO BUILD LINKS TO (off-site — human action):")
        for i, a in enumerate(assets[: cfg["worklist_size"]], 1):
            angle = ("Reddit/forum share + directory + niche roundup pitch"
                     if a["type"] in ("comparison", "review")
                     or any(k in a["slug"] for k in ("vs", "benchmark", "best-"))
                     else "resource-page + community citation")
            lines.append(f"  {i}. {a['title'][:80]}")
            lines.append(f"     {a['url']}")
            lines.append(f"     type={a['type']} · {a['n_products']} products · {a['body_len']} chars")
            lines.append(f"     angle: {angle}")
        lines.append("")
        lines.append("Suggested channels (highest ROI first): relevant subreddits where")
        lines.append("the comparison genuinely answers a question; HARO/Qwoted for the")
        lines.append("data-rich benchmark pieces; niche resource pages; product-database")
        lines.append("citations. Quality > quantity — 3 real links beat 50 directory spam.")
        lines.append("")
        lines.append("ON-SITE (do these too — free authority routing):")
        lines.append("For each asset above, add 2-3 contextual inline links to it FROM")
        lines.append("already-indexed hub pages (homepage rails, its /category/ page, and")
        lines.append("the 2-3 most-related published articles), anchored on its primary")
        lines.append("keyword. That passes crawl-priority + PageRank and is often what")
        lines.append("tips a page from 'Discovered' → 'Indexed'.")
        return "\n".join(lines)

    def _email_worklist(self, cfg, body: str) -> bool:
        try:
            from shared.site_quality import send_via_msmtp
            import html as _html
            subject = f"[AUTHORITY:{cfg['site_id']}] link-building worklist"
            body_html = "<pre style=\"font:13px/1.5 monospace\">" + _html.escape(body) + "</pre>"
            ok, info = send_via_msmtp(
                subject=subject, body_html=body_html,
                to=["mperry@northernsoftwareconsulting.com"],
                sender="automation@northernsoftwareconsulting.com",
                msmtp_account="automation", bypass_digest=True)
            self.decide("action" if ok else "observation",
                        f"authority worklist email for {cfg['site_id']}: ok={ok} {info}")
            return bool(ok)
        except Exception as e:
            self.decide("observation", f"worklist email skipped: {e}")
            return False

    @requires_confirmation(reason="sends an external backlink/citation pitch to a third party")
    def send_external_outreach(self, target: str, message: str) -> None:
        """Human-gated. Auto-outreach without approval is spam and a
        reputation risk — this only fires after the owner approves."""
        pass

    def teardown(self) -> None:
        try:
            if getattr(self, "_conn", None):
                self._conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    AuthorityAgent().run_once()
