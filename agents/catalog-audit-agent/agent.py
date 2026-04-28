#!/usr/bin/env python3
"""Catalog Audit Agent — generic blueprint for site-catalog quality audits.

Wraps an existing per-site audit script (typescript / shell) and converts
its findings into the standard reusable-agents recommendations.json schema
so the responder + implementer flow can apply fixes via email reply.

Configuration (via env-pointed YAML):
  CATALOG_AUDIT_CONFIG=/path/to/site.yaml

Site config (yaml) shape:
  site_id: aisleprompt
  what_we_do: "AI grocery / meal-planning catalog (recipes + kitchen products)"
  audit:
    command:
      - bash
      - "-c"
      - |
        cd "/path/to/site/repo" && \\
        DATABASE_URL="..." npx tsx scripts/catalog-quality-audit.ts --no-email
    findings_path: "/path/to/site/repo/audit-history"  # latest YYYY-MM-DD.json
    findings_format: aisleprompt-catalog-audit  # or 'specpicks-image-csv'
  reporter:
    email:
      from: "AislePrompt Audit <automation@northernsoftwareconsulting.com>"
      to: ["mperry@northernsoftwareconsulting.com"]
      subject_template: "[{agent_id}:{request_id}] Catalog Audit — {site} — {tag}"

Inherits all framework lessons from PI/CR work:
  - sys.path = parent.parent.parent (repo root, not agents/)
  - self.agent_id (per-site) for email subject + outbound metadata
  - send_via_msmtp uses Graph internally (msmtp is sandboxed by AppArmor)
  - Outbound metadata at agents/<agent_id>/outbound-emails/<request_id>.json
  - Responder routes [<agent_id>:<request_id>] subject tags to implementer
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# After agents/ consolidation: this file is at agents/catalog-audit-agent/,
# so the repo root is THREE levels up.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.email_codes import new_request_id  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

from shared.site_quality import (  # noqa: E402
    apply_user_responses,
    assign_rec_ids,
    dispatch_auto_recs,
    load_quality_config,
    render_recs_email,
    score_tier,
    send_via_msmtp,
    validate_recs_doc,
)


AGENT_ID = "catalog-audit-agent"


# ---------------------------------------------------------------------------
# Findings → recs converters
# ---------------------------------------------------------------------------

# Severity mapping from audit script vocabulary → standard schema vocabulary
_SEVERITY_MAP = {
    "critical": "critical", "fatal": "critical",
    "warn": "high", "warning": "high",
    "info": "medium", "notice": "medium",
    "low": "low",
}


def _findings_to_recs_aisleprompt(findings_doc: dict, max_recs: int = 30) -> list[dict]:
    """Convert aisleprompt catalog-quality-audit.ts output → recs."""
    findings = findings_doc.get("findings", [])
    recs: list[dict] = []
    for f in findings:
        count = int(f.get("count", 0))
        if count == 0:
            continue
        crit = f.get("criterionId", "unknown")
        sev = _SEVERITY_MAP.get(f.get("severity", "low"), "low")
        sample = f.get("sample", []) or []
        # Heuristic confidence: high when the audit caught it directly,
        # lower when it's a sampled subset (image url checks).
        conf = 0.95 if "valid-url" not in crit else 0.85

        # Goal mapping — derive from criterion id prefix
        goal_ids = []
        if "miscategor" in crit or "category-in-allow" in crit or "categor" in crit:
            goal_ids.append("goal-zero-miscategorized-products")
        if "image" in crit:
            goal_ids.append("goal-image-quality")
        if "duplicate" in crit:
            goal_ids.append("goal-zero-duplicate-content")
        if "title" in crit or "instructions" in crit or "ingredients" in crit:
            goal_ids.append("goal-content-completeness")

        # Concrete evidence: first 5 sample items with their ids + titles
        evidence = []
        for s in sample[:5]:
            if isinstance(s, dict):
                evidence.append({
                    "ref_id": str(s.get("id", "")),
                    "snippet": (s.get("title") or s.get("detail") or "")[:160],
                })

        title = f"Fix {count} {crit.replace('-', ' ')} issue(s)"
        recs.append({
            "category": crit,
            "check_id": crit,
            "severity": sev,
            "confidence": conf,
            "tier": score_tier(confidence=conf, severity=sev, threshold=0.92),
            "title": title[:160],
            "rationale": f"Catalog audit found {count} rows failing the '{crit}' criterion.",
            "evidence": evidence,
            "implementation_outline": {
                "approach": (
                    f"For each item flagged in the evidence list above (and any "
                    f"others matching criterion '{crit}'), edit the catalog data "
                    f"or the scraper to fix the underlying issue. See the "
                    f"audit script in scripts/catalog-quality-audit.ts for the "
                    f"exact SQL the criterion uses."
                ),
            },
            "implemented": False,
            "goal_ids": goal_ids,
        })
    return recs[:max_recs]


def _findings_to_recs_specpicks_images(findings_doc: dict, max_recs: int = 30) -> list[dict]:
    """Convert specpicks audit-product-images CSV → recs (one per mismatch)."""
    rows = findings_doc.get("rows", [])
    mismatches = [r for r in rows if r.get("verdict") == "mismatch"]
    recs: list[dict] = []
    for m in mismatches[:max_recs]:
        recs.append({
            "category": "image-name-mismatch",
            "check_id": "product-image-matches-name",
            "severity": "high",
            "confidence": 0.9,
            "tier": "review",
            "title": f"Image mismatch: {(m.get('name') or '')[:120]}",
            "rationale": (m.get("reason") or "")[:240],
            "evidence": [{
                "ref_id": m.get("slug", ""),
                "snippet": f"image_url: {m.get('image_url','')[:160]}",
            }],
            "implementation_outline": {
                "approach": (
                    "Either re-scrape the product image with a stricter "
                    "brand/model filter, or deactivate the product (UPDATE "
                    "products SET is_active=false WHERE slug=...). The exact "
                    "SQL is logged at the bottom of the original audit run."
                ),
            },
            "implemented": False,
            "goal_ids": ["goal-image-quality"],
        })
    return recs


CONVERTERS = {
    "aisleprompt-catalog-audit": _findings_to_recs_aisleprompt,
    "specpicks-image-csv": _findings_to_recs_specpicks_images,
}


# ---------------------------------------------------------------------------
# Audit input loaders — read the file the audit script wrote
# ---------------------------------------------------------------------------

def _latest_findings_aisleprompt(findings_dir: Path) -> dict | None:
    """The aisleprompt catalog-quality-audit writes audit-history/YYYY-MM-DD.json
    each day. Pick the newest file."""
    if not findings_dir.is_dir():
        return None
    files = sorted(findings_dir.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return None


def _latest_findings_specpicks_images(csv_glob: str) -> dict | None:
    """Specpicks audit-product-images writes audit-product-images-<ts>.csv. Pick
    the newest file matching the glob, parse to {rows: [...]}.
    """
    from glob import glob as _glob
    matches = sorted(_glob(csv_glob))
    if not matches:
        return None
    rows: list[dict] = []
    with open(matches[-1]) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return {"rows": rows, "source_csv": matches[-1]}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CatalogAuditAgent(AgentBase):
    agent_id = AGENT_ID
    send_run_summary_email = False  # already sends its own report email
    name = "Catalog Audit Agent"
    description = (
        "Runs a per-site catalog quality audit (recipes, products, listings, "
        "images), converts findings into recommendation IDs, and emails them "
        "with reply-syntax to dispatch fixes."
    )
    category = "research"

    capabilities = [
        declare("run_audit_script", "Execute the configured audit command",
                confirmation_required=False, risk_level="low"),
        declare("send_recommendations_email",
                "Send ranked findings via email to the configured recipient",
                confirmation_required=False, risk_level="low"),
    ]

    def __init__(self, *args, **kwargs):
        env_id = os.environ.get("AGENT_ID")
        if env_id and env_id != AGENT_ID:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        cfg_path = os.environ.get("CATALOG_AUDIT_CONFIG")
        if not cfg_path:
            raise SystemExit("CATALOG_AUDIT_CONFIG env var not set")
        if not Path(cfg_path).is_file():
            raise SystemExit(f"config not found: {cfg_path}")
        # load_quality_config gives us the .reporter / .site_id / .label structure
        # we already use for PI/CR; we just plug an `audit:` section into the same
        # SiteQualityConfig.
        self.cfg = load_quality_config(cfg_path)
        self.run_dir = self.cfg.run_dir_for_now(self.agent_id)
        self.decide("setup",
                    f"site={self.cfg.site_id} run_dir={self.run_dir} "
                    f"agent_id={self.agent_id}",
                    evidence={"site": self.cfg.site_id})

    def run(self) -> RunResult:
        cfg = self.cfg
        audit = cfg.get("audit") or {}
        cmd = audit.get("command")
        fmt = audit.get("findings_format", "aisleprompt-catalog-audit")

        # ── 1. Run the audit script (best-effort — many audit scripts already
        #      run on cron and write their findings file, so a fresh exec
        #      isn't strictly required) ─────────────────────────────────────
        if cmd:
            self.status("running audit script", progress=0.1, current_action="audit")
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=int(audit.get("timeout_s", 1800)),
                )
                self.decide("observation",
                            f"audit rc={proc.returncode} stdout={len(proc.stdout)}b "
                            f"stderr={len(proc.stderr)}b")
                if proc.returncode != 0:
                    self.decide("error",
                                f"audit script rc={proc.returncode}: "
                                f"{(proc.stderr or proc.stdout)[:300]}")
            except Exception as e:
                self.decide("error", f"audit exec failed: {e}")
        else:
            self.decide("observation",
                        "no audit.command configured — using existing findings file")

        # ── 2. Load latest findings ────────────────────────────────────────
        self.status("loading findings", progress=0.4, current_action="load")
        if fmt == "aisleprompt-catalog-audit":
            findings_dir = Path(audit.get("findings_path", ""))
            findings_doc = _latest_findings_aisleprompt(findings_dir)
        elif fmt == "specpicks-image-csv":
            csv_glob = audit.get("findings_glob", "")
            findings_doc = _latest_findings_specpicks_images(csv_glob)
        else:
            raise SystemExit(f"unsupported findings_format: {fmt!r}")
        if not findings_doc:
            return RunResult(status="failure", summary="no findings file found",
                              metrics={})

        # ── 3. Convert to recs ─────────────────────────────────────────────
        self.status("converting findings → recs", progress=0.55, current_action="convert")
        recs = CONVERTERS[fmt](findings_doc, max_recs=int(audit.get("max_recs", 30)))
        if not recs:
            return RunResult(status="success",
                              summary="audit found 0 actionable issues — clean run",
                              metrics={"recs_total": 0})
        recs = sorted(recs, key=lambda r: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(r["severity"], 9),
            -r["confidence"],
        ))
        recs = assign_rec_ids(recs)

        # ── 4. Drain user replies from prior run, write recommendations.json ─
        prior_recs_path = self._most_recent_recs_path()
        applied = apply_user_responses(
            responses=getattr(self, "responses", []) or [],
            prior_recs_path=prior_recs_path,
        )
        if applied:
            self.decide("observation",
                        f"applied {len(applied)} user response(s) to prior recs",
                        evidence={"applied": applied})

        request_id = new_request_id()
        recs_doc = {
            "schema_version": "1",
            "site": cfg.site_id,
            "agent": self.agent_id,
            # Duplicate as `agent_id` so the implementer dispatcher (which
            # reads `agent_id` to pick the runbook) matches without a
            # legacy-key fallback. PI/CR don't strictly need this because
            # their dispatch is the default seo/AGENT.md path; catalog-audit
            # needs the catalog-audit branch + CATALOG_AUDIT.md runbook.
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "summary": (
                f"Catalog audit on {cfg.label} found {len(recs)} actionable issue(s) "
                f"(critical={sum(1 for r in recs if r['severity']=='critical')}, "
                f"high={sum(1 for r in recs if r['severity']=='high')}, "
                f"medium={sum(1 for r in recs if r['severity']=='medium')}, "
                f"low={sum(1 for r in recs if r['severity']=='low')})."
            ),
            "recommendations": recs,
        }
        validate_recs_doc(recs_doc)
        self._save_artifact("recommendations.json", recs_doc)

        # ── 5. Email — use self.agent_id (per-site) so the responder routes ─
        self.status("rendering email", progress=0.85, current_action="email")
        subject, html = render_recs_email(
            cfg=cfg, agent_id=self.agent_id, request_id=request_id,
            recs=recs, summary=recs_doc["summary"],
        )
        self._save_artifact("email-rendered.html", html)

        email_cfg = (cfg.get("reporter", {}) or {}).get("email") or {}
        to = email_cfg.get("to") or []
        sender = email_cfg.get("from", "")
        msmtp_account = email_cfg.get("msmtp_account", "automation")
        if to and sender:
            ok, detail = send_via_msmtp(
                subject=subject, body_html=html, to=to,
                sender=sender, msmtp_account=msmtp_account,
                extra_headers={
                    "X-Reusable-Agent": self.agent_id,
                    "Reply-To": sender,
                },
            )
            if ok:
                self.decide("action",
                            f"emailed {len(to)} recipient(s) ({detail})")
                # Outbound metadata so responder can match the reply
                self.storage.write_json(
                    f"agents/{self.agent_id}/outbound-emails/{request_id}.json",
                    {
                        "schema_version": "1",
                        "request_id": request_id,
                        "agent_id": self.agent_id,
                        "site": cfg.site_id,
                        "run_ts": self.run_ts,
                        "subject": subject,
                        "to": list(to),
                        "expects_response": True,
                        "rec_count": len(recs),
                        "rec_ids": [r["id"] for r in recs],
                        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "transport": detail,
                        "ok": True,
                        "kind": "email-recommendations",
                    },
                )
            else:
                self.decide("error", f"email send failed: {detail}")
        else:
            self.decide("observation", "no recipient/sender configured")

        # ── 6. Auto-dispatch tier=auto recs (if opted in) ───────────────────
        dispatched = dispatch_auto_recs(
            cfg=cfg, agent_id=self.agent_id, recs=recs, storage=self.storage,
        )
        if dispatched:
            self.decide("action",
                        f"auto-dispatched {len(dispatched)} recs to implementer",
                        evidence={"rec_ids": dispatched})

        return RunResult(
            status="success",
            summary=recs_doc["summary"],
            metrics={
                "recs_total": len(recs),
                "recs_critical": sum(1 for r in recs if r["severity"] == "critical"),
                "recs_high": sum(1 for r in recs if r["severity"] == "high"),
                "applied_responses": len(applied),
                "auto_dispatched": len(dispatched),
            },
            next_state={
                "last_run_ts": self.run_ts,
                "last_request_id": request_id,
                "site_id": cfg.site_id,
            },
        )

    def _most_recent_recs_path(self) -> Path | None:
        """Find the most recent prior run's recommendations.json for this site."""
        site_runs = self.run_dir.parent  # runs_root/<site>/
        if not site_runs.is_dir():
            return None
        candidates = sorted(
            p for p in site_runs.iterdir()
            if p.is_dir() and p.name != self.run_ts and (p / "recommendations.json").is_file()
        )
        if not candidates:
            return None
        return candidates[-1] / "recommendations.json"

    def _save_artifact(self, name: str, content: Any) -> None:
        storage_key = f"agents/{self.agent_id}/runs/{self.run_ts}/{name}"
        disk = self.run_dir / name
        if isinstance(content, (dict, list)):
            disk.write_text(json.dumps(content, indent=2))
            self.storage.write_json(storage_key, content)
        else:
            text = str(content)
            disk.write_text(text)
            self.storage.write_text(storage_key, text)


if __name__ == "__main__":
    CatalogAuditAgent().run_once()
