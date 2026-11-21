#!/usr/bin/env python3
"""Publish article drafts only when every guard passes: publishing as a gated
state change (third-site design, binding amendment 1; build item F6a).

Why: the Opus author INSERTs its own rows, and until now it wrote them as
status='published', so they skipped the wrapper's INSERT-time guards and the
claims sweep only reported afterwards. An article that says "backfeed through
the dryer outlet" or quotes CPAP runtimes would go live. For a site that opts
into a content policy (config/content-policy-config.json), the site database
carries a trigger that forces every write by a role other than the publisher
out of the published state (kit migration template, build item K4), and this
gate is the only path to 'published'.

STATUSES. publish_gate.draft_status (default 'pending') is the gate's QUEUE:
the trigger writes every author INSERT, and every author edit of a live
article's text, as 'pending' (render the K4 template with that draftStatus).
'draft', 'archived' and any other status are HOLDS the gate never selects or
promotes: an operator or agent takes an article down by setting status to
'draft' (or 'archived'), and resubmits it to the gate by setting 'pending'
(the trigger keeps a hold held when an author write asks for 'published').

  1. Select the site's pending rows (all of them, newest first, up to
     `limit`; --drafts-since-hours narrows that to a window). The status is
     the queue, so a live article an agent edited without bumping updated_at
     is still re-checked and re-published.
  2. Run, per row: the inline-link guard (article_link_guard, the site's
     resolved config, catalog check when configured, bracketed link text),
     the link classes (article_link_classes), the claims guard
     (article_claims_guard) and the content policy (content_policy). The
     guards are called through their public functions; none is modified.
  3. --commit: promote passing rows through a SEPARATE publisher-role
     connection (compare-and-set on the checked content, so a body edited
     after the check is never published unchecked). published_at is the
     FIRST GO-LIVE TIME: the K4 trigger clears whatever date an author INSERT
     wrote and never lets another role clear it later, and the promote sets
     COALESCE(published_at, now()), so a draft held for days goes live dated
     the day it went live while an edited or resubmitted live article keeps
     its first date. Re-queue failing rows through
     framework.core.implementation_queue as Opus article-refresh recs whose
     addendum names every violation.
  4. --enforce: also check published rows written in the --since-hours window
     (0 = every published row) and (with --commit) return the ones that break
     an enforced check (default: claims guard and content policy; link minima
     never unpublish) to the queue, then re-queue them.

Fail closed: before any write (--commit) the gate checks that it is talking
to the site's own database: a site-specific author DSN (never the generic
DATABASE_URL unless --dsn-env names it), publish_gate.database_name when
configured, the same database on the author and publisher connections, and
the K4 publish guard installed, enabled and written for this gate's statuses.
No publisher DSN, a publisher connection that is not the publisher role (the
trigger keeps the row pending), a config that does not compile, an unreadable
storage copy of it, or any guard error leaves the row pending. A site that has
not opted in is untouched (exit 0, nothing selected), so every existing site is
unaffected unless it opts in.

    # what would happen (read-only; default)
    python3 -m framework.cli.article_publish_gate --site <site>
    # the implementer's post-author hook (build item W2)
    python3 -m framework.cli.article_publish_gate --site <site> --commit
    # the enforcing sweep over every published row
    python3 -m framework.cli.article_publish_gate --site <site> --enforce --since-hours 0 --commit
    # self-check of one row after an edit (read-only; prints PASS or FAIL)
    python3 -m framework.cli.article_publish_gate --site <site> --slug <slug>

DSNs (never on the command line; env var NAMES only): the author DSN from
--dsn-env, else the site's dsn_env in config/article-heading-repair-config,
else DATABASE_URL_<SITE>, else (read-only runs only) DATABASE_URL. The
publisher DSN from --publisher-dsn-env, else publish_gate.publisher_dsn_env,
else <dsn_env>_PUBLISHER, else DATABASE_URL_<SITE>_PUBLISHER.

Exit codes: 0 done (or nothing to do); 1 some row failed and
--fail-on-reject was given; 2 configuration error (nothing was promoted).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from framework.core import article_claims_guard as claims_guard  # noqa: E402
from framework.core import article_link_guard as link_guard  # noqa: E402
from framework.core import content_policy  # noqa: E402
from framework.core.article_link_classes import (  # noqa: E402
    LinkClassConfigError,
    verify_with_guard_config,
)

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
CHECKS = ("link_guard", "link_classes", "claims_guard", "content_policy")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_STATUS = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")
_DBNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$-]{0,62}$")
_FALLBACK_GATE = {
    "draft_status": "pending", "published_status": "published", "window_hours": 6, "drafts_window_hours": 0,
    "window_column": "", "publisher_dsn_env": "", "publisher_role": "", "database_name": "",
    "guard_function": "{table}_publish_guard", "verify_guard": True,
    "requeue_agent_id": "{site}-article-publish-gate", "requeue_subject_tag": "refresh", "requeue_max_attempts": 3,
    "enforce_checks": ["claims_guard", "content_policy"], "written_by_like": [], "faqs_column": "faqs",
    "limit": 200,
}


class GateConfigError(RuntimeError):
    """The gate cannot run safely; nothing is promoted."""



# ── settings ────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class GateSettings:
    site: str                     # the content-policy site key ("" = not opted in)
    hint: str
    enabled: bool
    table: str
    slug_column: str
    body_column: str
    status_column: str
    window_column: str
    draft_status: str
    published_status: str
    window_hours: float
    dsn_env: str
    publisher_dsn_env: str
    publisher_role: str
    requeue_agent_id: str
    requeue_subject_tag: str
    requeue_max_attempts: int
    enforce_checks: tuple[str, ...]
    written_by_like: tuple[str, ...]
    faqs_column: str
    limit: int
    # Added after the first build; defaulted so existing callers that
    # construct GateSettings directly keep working.
    drafts_window_hours: float = 0.0       # pending rows: 0 = all (the status is the queue)
    database_name: str = ""               # when set, both connections must report it
    guard_function: str = ""              # the K4 publish guard's function ("" = any trigger)
    verify_guard: bool = True             # refuse to write without that guard

    @property
    def site_env(self) -> str:
        return re.sub(r"[^A-Z0-9]+", "_", (self.site or self.hint).upper()).strip("_")


def _heading_settings(site_hint: str) -> dict:
    """Table / column / DSN-env mapping shared with the other article sweeps
    (config/article-heading-repair-config.json)."""
    from framework.cli.article_heading_repair import resolve_settings
    return resolve_settings(site_hint)


def resolve_gate_settings(site_hint: str, cp_config: Mapping[str, Any], policy: content_policy.ContentPolicy,
                          table_settings: Mapping[str, Any] | None = None) -> GateSettings:
    """Gate settings: content-policy config `default.publish_gate` with the
    site block's `publish_gate` laid over it; table and columns from the
    article-heading-repair mapping. The gate is enabled for a site that opted
    into a rule set unless its publish_gate says enabled=false (or a site may
    enable it with no rule set)."""
    key, block = content_policy.site_block(cp_config, site_hint)
    gate = dict(_FALLBACK_GATE)
    gate.update(((cp_config.get("default") or {}).get("publish_gate")) or {})
    gate.update(block.get("publish_gate") or {})
    st = dict(table_settings if table_settings is not None else _heading_settings(key or site_hint))
    for k in ("table", "slug_column", "body_column", "status_column", "updated_column"):
        if not _IDENT.match(str(st.get(k) or "")):
            raise GateConfigError(f"article-heading-repair-config {k}={st.get(k)!r} is not a SQL identifier")
    window_col = str(gate.get("window_column") or st["updated_column"])
    faqs_col = str(gate.get("faqs_column") or "")
    for k, v in (("window_column", window_col), ("faqs_column", faqs_col)):
        if v and not _IDENT.match(v):
            raise GateConfigError(f"publish_gate.{k}={v!r} is not a SQL identifier")
    enforce = tuple(str(c) for c in (gate.get("enforce_checks") or ()))
    unknown = [c for c in enforce if c not in CHECKS]
    if unknown:
        raise GateConfigError(f"publish_gate.enforce_checks: unknown check(s) {unknown} (known: {CHECKS})")
    draft, published = str(gate.get("draft_status") or ""), str(gate.get("published_status") or "")
    for k, v in (("draft_status", draft), ("published_status", published)):
        if not _STATUS.match(v):
            raise GateConfigError(f"publish_gate.{k}={v!r} must be 1-20 of a-z, 0-9, '_' or '-'")
    if draft == published:
        raise GateConfigError("publish_gate.draft_status and published_status must differ")
    guard_fn = str(gate.get("guard_function") or "").replace("{table}", str(st["table"]))
    if guard_fn and not _IDENT.match(guard_fn):
        raise GateConfigError(f"publish_gate.guard_function={guard_fn!r} is not a SQL identifier")
    dbname = str(gate.get("database_name") or "")
    if dbname and not _DBNAME.match(dbname):
        raise GateConfigError(f"publish_gate.database_name={dbname!r} is not a database name")
    site = key or ""
    agent = str(gate.get("requeue_agent_id") or "{site}-article-publish-gate").replace("{site}", site or "site")
    if not _AGENT_ID.match(agent):
        raise GateConfigError(f"publish_gate.requeue_agent_id={agent!r} is not a kebab-case agent id")
    enabled = bool(site) and bool(gate.get("enabled", policy.active))
    return GateSettings(
        site=site, hint=site_hint, enabled=enabled, table=st["table"], slug_column=st["slug_column"],
        body_column=st["body_column"], status_column=st["status_column"], window_column=window_col,
        draft_status=draft, published_status=published,
        window_hours=float(gate.get("window_hours") or 0), dsn_env=str(st.get("dsn_env") or "DATABASE_URL"),
        publisher_dsn_env=str(gate.get("publisher_dsn_env") or ""), publisher_role=str(gate.get("publisher_role") or ""),
        requeue_agent_id=agent, requeue_subject_tag=str(gate.get("requeue_subject_tag") or "refresh"),
        requeue_max_attempts=int(gate.get("requeue_max_attempts") or 3), enforce_checks=enforce,
        written_by_like=tuple(str(x) for x in (gate.get("written_by_like") or ())), faqs_column=faqs_col,
        limit=int(gate.get("limit") or 200), drafts_window_hours=float(gate.get("drafts_window_hours") or 0),
        database_name=dbname, guard_function=guard_fn, verify_guard=bool(gate.get("verify_guard", True)))


def author_dsn_candidates(st: GateSettings, flag: str = "", *, writes: bool = False) -> list[str]:
    """The site's own variable before the generic DATABASE_URL (which the
    implementer points at the dispatch's site, but an operator shell may not).
    A run that writes (`writes`: --commit unpublishes through this DSN) never
    falls back to the generic DATABASE_URL: only `flag` (--dsn-env) may name
    it, deliberately."""
    names = [flag, st.dsn_env if st.dsn_env != "DATABASE_URL" else "",
             f"DATABASE_URL_{st.site_env}" if st.site_env else "", "" if writes else "DATABASE_URL"]
    return [n for n in dict.fromkeys(names) if n]


def publisher_dsn_candidates(st: GateSettings, flag: str = "") -> list[str]:
    """Never a generic DATABASE_URL_PUBLISHER: that could be another site's."""
    names = [flag, st.publisher_dsn_env,
             f"{st.dsn_env}_PUBLISHER" if st.dsn_env and st.dsn_env != "DATABASE_URL" else "",
             f"DATABASE_URL_{st.site_env}_PUBLISHER" if st.site_env else ""]
    return [n for n in dict.fromkeys(names) if n]


def first_env(names: Sequence[str]) -> tuple[str, str]:
    """(env var name, value) of the first set variable, else ("", "")."""
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return n, v
    return "", ""


# ── the checks ──────────────────────────────────────────────────────────────

@dataclasses.dataclass
class CheckResult:
    name: str
    passes: bool
    reason: str = ""
    addendum: str = ""
    warnings: list[str] = dataclasses.field(default_factory=list)
    details: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"name": self.name, "passes": self.passes}
        for k in ("reason", "warnings", "details"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


@dataclasses.dataclass
class GateContext:
    site_hint: str
    link_cfg: dict
    claims_cfg: dict
    policy: content_policy.ContentPolicy
    slug_ok: Callable[[str], bool] | None = None


def fingerprint(row: Mapping[str, Any]) -> str:
    """Hash of everything the checks read, so a re-queue is not repeated for
    an unchanged body and a promotion can tell what it checked."""
    parts = [str(row.get(k) or "") for k in ("body_md", "title", "subtitle", "excerpt")]
    parts.append(json.dumps(row.get("faqs"), sort_keys=True, default=str))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def _link_guard_check(row: Mapping[str, Any], ctx: GateContext) -> CheckResult:
    body = row.get("body_md") or ""
    buckets = [str(row.get("bucket") or ""), str(row.get("category") or "")]
    audit = link_guard.verify_body_with_config(body, {}, ctx.link_cfg, bucket=buckets,
                                               title=str(row.get("title") or ""), slug_ok=ctx.slug_ok)
    brackets = link_guard.find_bracketed_anchors(body)
    details = {"recipe_links": audit.recipe_links, "kitchen_links": audit.kitchen_links,
               "product_links": audit.product_links, "buy_links": audit.buy_links,
               "min_buy_links": audit.min_buy_links, "bracketed_anchors": len(brackets)}
    if audit.passes and not brackets:
        return CheckResult("link_guard", True, details=details)
    reasons, add = [], []
    if not audit.passes:
        reasons.append(audit.failure_reason())
        add.append(link_guard.render_failure_addendum(audit))
    if brackets:
        reasons.append(f"{len(brackets)} link text(s) hold square brackets")
        add.append("LINK TEXT WITH BRACKETS — the site renders these as raw markdown. Use parentheses "
                   "inside link text and never put a link inside another link's text: "
                   + "; ".join(f"[{t[:80]}]" for t in brackets[:5]))
    return CheckResult("link_guard", False, "; ".join(reasons), "\n".join(add), details=details)


def _link_classes_check(row: Mapping[str, Any], ctx: GateContext) -> CheckResult:
    try:
        audit = verify_with_guard_config(row.get("body_md") or "", ctx.link_cfg)
    except LinkClassConfigError as e:
        return CheckResult("link_classes", False, f"link_classes config invalid: {e}",
                           "LINK CLASSES — the site's link_classes config is invalid; an operator must fix "
                           "config/article-link-guard-config.json before this article can publish.")
    if audit.passes:
        return CheckResult("link_classes", True, details=audit.to_dict()["counts"])
    return CheckResult("link_classes", False, audit.failure_reason(), audit.addendum(), details=audit.counts)


def _claims_check(row: Mapping[str, Any], ctx: GateContext) -> CheckResult:
    bucket = str(row.get("bucket") or row.get("category") or "")
    pol = claims_guard.resolve_policy(ctx.site_hint, bucket, config=ctx.claims_cfg)
    audit = claims_guard.check(row.get("body_md") or "", subtitle=str(row.get("subtitle") or ""),
                               excerpt=str(row.get("excerpt") or ""), bucket=bucket, policy=pol)
    reason = audit.failure_reason()
    return CheckResult("claims_guard", audit.passes, reason, reason, warnings=audit.warnings())


def _content_policy_check(row: Mapping[str, Any], ctx: GateContext) -> CheckResult:
    audit = content_policy.check_article(
        ctx.policy, body_md=row.get("body_md") or "", title=str(row.get("title") or ""),
        subtitle=str(row.get("subtitle") or ""), excerpt=str(row.get("excerpt") or ""), faqs=row.get("faqs"))
    warns = [f"{v.rule_id}: {v.excerpt[:100]!r}" for v in audit.warnings]
    return CheckResult("content_policy", audit.passes, audit.failure_reason(),
                       audit.addendum() if not audit.passes else "", warns,
                       details={"rejects": [v.to_dict() for v in audit.rejects]} if audit.rejects else {})


_CHECK_FUNCS = {"link_guard": _link_guard_check, "link_classes": _link_classes_check,
                "claims_guard": _claims_check, "content_policy": _content_policy_check}


@dataclasses.dataclass
class RowVerdict:
    slug: str
    status: str
    title: str
    fingerprint: str
    checks: list[CheckResult]
    action: str = ""              # promote | hold | unpublish | keep | promoted | ... (set by the run)
    note: str = ""

    @property
    def passes(self) -> bool:
        return all(c.passes for c in self.checks)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.passes]

    def fails_any(self, names: Iterable[str]) -> bool:
        names = set(names)
        return any(not c.passes and c.name in names for c in self.checks)

    def addendum(self) -> str:
        parts = [c.addendum or f"{c.name.upper()}: {c.reason}" for c in self.checks if not c.passes]
        return "\n\n".join(p for p in parts if p)

    def to_dict(self) -> dict:
        return {"slug": self.slug, "status": self.status, "title": self.title, "fingerprint": self.fingerprint,
                "passes": self.passes, "failed": self.failed, "action": self.action, "note": self.note,
                "checks": [c.to_dict() for c in self.checks]}


def evaluate_row(row: Mapping[str, Any], ctx: GateContext) -> RowVerdict:
    """Every check on one row. A check that raises fails (fail closed)."""
    results = []
    for name in CHECKS:
        try:
            results.append(_CHECK_FUNCS[name](row, ctx))
        except Exception as e:  # noqa: BLE001 - a broken guard never publishes
            results.append(CheckResult(name, False, f"{name} error: {type(e).__name__}: {str(e)[:200]}",
                                       f"{name.upper()}: the check could not run ({type(e).__name__}); "
                                       f"an operator must look at it before this article can publish."))
    return RowVerdict(str(row.get("slug") or ""), str(row.get("status") or ""), str(row.get("title") or ""),
                      fingerprint(row), results)


# ── database ────────────────────────────────────────────────────────────────

def _import_psycopg2():
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        # claude-pool remaps HOME, so the user site-packages dir can fall off
        # sys.path; probe the env and passwd homes (as the other sweeps do).
        import glob
        import pwd
        for home in filter(None, {os.path.expanduser("~"), pwd.getpwuid(os.getuid()).pw_dir}):
            for cand in glob.glob(f"{home}/.local/lib/python3.*/site-packages"):
                if cand not in sys.path:
                    sys.path.append(cand)
    import psycopg2
    return psycopg2


def _connect(dsn: str, readonly: bool = True):
    """A psycopg2 connection with keepalives and a 30 s statement timeout;
    read-only + autocommit unless the caller writes."""
    _import_psycopg2()
    from framework.core.pg_conn import connect
    conn = connect(dsn)
    if readonly:
        conn.set_session(readonly=True, autocommit=True)
    else:
        conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 30000")
        cur.execute("SET lock_timeout = 5000")
    if not readonly:
        conn.commit()
    return conn


class ArticleStore:
    """Every statement the gate runs, on one connection. Identifiers come from
    validated config; values are always bound parameters."""

    def __init__(self, conn, st: GateSettings):
        self.conn, self.st = conn, st
        self._cols: dict[str, str] | None = None

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    def columns(self) -> dict[str, str]:
        if self._cols is None:
            with self.conn.cursor() as cur:
                cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_name = %s AND table_schema = ANY(current_schemas(false))",
                            (self.st.table,))
                self._cols = {r[0]: r[1] for r in cur.fetchall()}
        return self._cols

    def try_lock(self) -> bool:
        """A session-level advisory lock, so two gate runs never act on the
        same site at once (held until the connection closes)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"article_publish_gate:{self.st.site}",))
            return bool(cur.fetchone()[0])

    def current_user(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT current_user")
            return str(cur.fetchone()[0])

    def current_database(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            return str(cur.fetchone()[0])

    def guard_status(self) -> dict:
        """The publish guard on the table (the kit's publish-trigger
        template, K4): a non-internal trigger running `guard_function` (any
        trigger when unset) that is enabled, and whose source names this
        gate's draft and published statuses. A database without it is not a
        gated site database: the gate refuses to write there."""
        st = self.st
        with self.conn.cursor() as cur:
            cur.execute("SELECT p.proname, p.prosrc, t.tgenabled FROM pg_trigger t "
                        "JOIN pg_proc p ON p.oid = t.tgfoid "
                        "WHERE t.tgrelid = to_regclass(%s) AND NOT t.tgisinternal", (st.table,))
            rows = [(str(n), str(src or ""), str(en)) for n, src, en in cur.fetchall()]
        mine = [r for r in rows if not st.guard_function or r[0] == st.guard_function]
        enabled = [r for r in mine if r[2] in ("O", "A")]
        statuses = [r for r in enabled if f"'{st.draft_status}'" in r[1] and f"'{st.published_status}'" in r[1]]
        return {"installed": bool(mine), "enabled": bool(enabled), "statuses": bool(statuses),
                "function": st.guard_function or (mine[0][0] if mine else "")}

    def select(self, status: str, *, since_hours: float, slugs: Sequence[str] = (), limit: int = 200) -> list[dict]:
        st, cols = self.st, self.columns()
        if not cols:
            raise GateConfigError(f"table {st.table!r} not found")
        for c in (st.slug_column, st.body_column, st.status_column, st.window_column):
            if c not in cols:
                raise GateConfigError(f"column {st.table}.{c} not found")
        alias = {st.slug_column: "slug", st.body_column: "body_md", st.status_column: "status"}
        if st.faqs_column:
            alias[st.faqs_column] = "faqs"
        wanted = [st.slug_column, st.body_column, st.status_column, "title", "subtitle", "excerpt", "bucket",
                  "category", st.faqs_column, "written_by", "published_at", st.window_column]
        sel = [c for c in dict.fromkeys(w for w in wanted if w) if c in cols]
        sql = (f"SELECT {', '.join(sel)} FROM {st.table} WHERE {st.status_column} = %(status)s")
        params: dict[str, Any] = {"status": status, "limit": int(limit)}
        if since_hours:
            sql += f" AND {st.window_column} >= now() - (%(hours)s * interval '1 hour')"
            params["hours"] = float(since_hours)
        if slugs:
            sql += f" AND {st.slug_column} = ANY(%(slugs)s)"
            params["slugs"] = list(slugs)
        if st.written_by_like and "written_by" in cols:
            sql += " AND written_by LIKE ANY(%(wb)s)"
            params["wb"] = list(st.written_by_like)
        sql += f" ORDER BY {st.window_column} DESC NULLS LAST LIMIT %(limit)s"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        return [{alias.get(k, k): v for k, v in r.items()} for r in rows]

    def _cas(self, row: Mapping[str, Any], params: dict) -> list[str]:
        """WHERE terms that match only the content the checks read."""
        st, cols = self.st, self.columns()
        where = [f"{st.slug_column} = %(slug)s", f"{st.body_column} = %(body)s"]
        params.update(slug=row.get("slug"), body=row.get("body_md") or "")
        for c in ("title", "subtitle", "excerpt"):
            if c in cols:
                where.append(f"COALESCE({c}, '') = %({c})s")
                params[c] = str(row.get(c) or "")
        if st.faqs_column and cols.get(st.faqs_column) in ("json", "jsonb"):
            where.append(f"{st.faqs_column}::jsonb IS NOT DISTINCT FROM %(faqs)s::jsonb")
            params["faqs"] = None if row.get("faqs") is None else json.dumps(row.get("faqs"), default=str)
        return where

    def _status_now(self, slug: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT {self.st.status_column} FROM {self.st.table} WHERE {self.st.slug_column} = %s",
                        (slug,))
            r = cur.fetchone()
        return str(r[0]) if r else ""

    def promote(self, row: Mapping[str, Any]) -> str:
        """draft_status (the queue) -> published, only if the row still holds
        what was checked. published_at is the first go-live time: stamped on
        the first promotion (the trigger leaves an author INSERT's NULL) and
        kept on every later one.
        Returns published | already-published | changed | gone |
        trigger-kept-draft (this connection is not the publisher role)."""
        st, cols = self.st, self.columns()
        params: dict[str, Any] = {"published": st.published_status, "draft": st.draft_status}
        sets = [f"{st.status_column} = %(published)s"]
        if "published_at" in cols:
            sets.append("published_at = COALESCE(published_at, now())")
        where = self._cas(row, params) + [f"{st.status_column} = %(draft)s"]
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {st.table} SET {', '.join(sets)} WHERE {' AND '.join(where)} "
                        f"RETURNING {st.status_column}", params)
            got = cur.fetchone()
        if got is None:
            self.conn.rollback()
            now = self._status_now(str(row.get("slug")))
            return {"": "gone", st.published_status: "already-published"}.get(now, "changed")
        if str(got[0]) != st.published_status:
            self.conn.rollback()          # the trigger forced it back; keep published_at untouched too
            return "trigger-kept-draft"
        self.conn.commit()
        return "published"

    def unpublish(self, row: Mapping[str, Any]) -> str:
        """published -> draft_status (back to the gate's queue, so the fixed
        row is re-checked and re-published), only if the row still holds what
        was checked. Returns unpublished | changed."""
        st = self.st
        params: dict[str, Any] = {"published": st.published_status, "draft": st.draft_status}
        where = self._cas(row, params) + [f"{st.status_column} = %(published)s"]
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {st.table} SET {st.status_column} = %(draft)s WHERE {' AND '.join(where)} "
                        f"RETURNING {st.status_column}", params)
            got = cur.fetchone()
        if got is None:
            self.conn.rollback()
            return "changed"
        self.conn.commit()
        return "unpublished"


def _open_store(dsn: str, readonly: bool, st: GateSettings) -> ArticleStore:
    return ArticleStore(_connect(dsn, readonly=readonly), st)


def _storage():
    """The framework storage backend (re-queue + ledger), or None."""
    try:
        from framework.core.storage import get_storage
        return get_storage()
    except (Exception, SystemExit):  # unknown STORAGE_BACKEND raises SystemExit
        return None


# ── re-queue ────────────────────────────────────────────────────────────────

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def build_requeue_rec(rec_id: str, v: RowVerdict, st: GateSettings, *, attempt: int, unpublished: bool,
                      checked_at: str) -> dict:
    """An Opus article-refresh rec: fix the row in place, every violation
    named. The publish gate re-checks it after the session."""
    state = ("was unpublished by the publish gate" if unpublished
             else f"is a '{st.draft_status}' row the publish gate did not publish")
    checks = []
    if "content_policy" in v.failed:
        checks.append(f"`python3 -m framework.cli.content_policy_scan --site {st.site} --format markdown "
                      f"<new body .md file>` must exit 0 (it lists every sentence the content policy rejects)")
    if "link_guard" in v.failed:
        checks.append(f"`python3 framework/cli/article_link_guard.py check --site {st.site} --slug {v.slug} "
                      f"--body <new body .md file>` must exit 0")
    checks.append(f"after the UPDATE, `python3 -m framework.cli.article_publish_gate --site {st.site} --slug "
                  f"{v.slug}` (read-only) must print PASS for the row; it runs every check the gate runs")
    steps = (
        f"The article `{v.slug}` {state} because it fails: {', '.join(v.failed)}. Rewrite it IN PLACE "
        f"(UPDATE the same {st.table} row and slug; never INSERT a new article, never change the slug). Use "
        f"Claude Opus for every word of prose (required_model_tier=opus; defer rather than fall back). Fix "
        f"every item in the addendum below without deleting unrelated prose, and bump updated_at in the same "
        f"UPDATE that changes the body. Self-check before you finish: " + "; ".join(checks) + ". Do NOT set "
        f"status yourself: the row stays '{st.draft_status}', and the publish gate publishes it after your "
        f"session when every check passes. DB-only change: commit the apply script you used and write "
        f"applied-recs.json (implementer runbook, DB-only recs).")
    addendum = "PUBLISH GATE — NOT PUBLISHED.\n\n" + v.addendum()
    return {
        "id": rec_id,
        "type": "article-refresh", "rec_type": "article-refresh", "kind": "article-refresh",
        "dispatch_kind": "article-refresh",
        "trigger": "publish-gate",
        "required_model_tier": "opus",
        "category": "content-policy",
        "priority": "high", "severity": "high", "tier": "auto-implement", "confidence": 1.0,
        "site": st.site,
        "title": f"Publish gate: fix {'unpublished article' if unpublished else 'draft'} "
                 f"'{(v.title or v.slug)[:90]}' ({', '.join(v.failed)})",
        "rationale": steps + "\n\n" + addendum,
        "addendum": addendum,
        "article_slug": v.slug,
        "article_status": "unpublished" if unpublished else st.draft_status,
        "publish_gate": {"failed_checks": v.failed, "fingerprint": v.fingerprint, "checked_at": checked_at,
                         "attempt": attempt, "checks": [c.to_dict() for c in v.checks if not c.passes]},
        "target_files": [],
        "expected_metric": "drafts_promoted",
        "attempt": attempt,
    }


def requeue(verdicts: Sequence[RowVerdict], st: GateSettings, storage, *, commit: bool,
            unpublished: Iterable[str] = (), now: _dt.datetime | None = None) -> dict:
    """Re-queue failing rows through implementation_queue.queue_recs. A row
    already re-queued for the same content is skipped, and a row past
    `requeue_max_attempts` is reported instead. Dry run unless `commit`."""
    from framework.core.implementation_queue import queue_recs

    now = now or _now()
    run_ts = now.strftime("%Y%m%dT%H%M%SZ")
    unpublished = set(unpublished)
    out: dict = {"run_ts": run_ts, "queued": [], "skipped": [], "exhausted": [], "request_id": ""}
    if not verdicts:
        return out
    ledger_key = f"agents/{st.requeue_agent_id}/state/requeued.json"
    ledger: dict = {}
    if storage is not None:
        try:
            ledger = storage.read_json(ledger_key) or {}
        except Exception:  # noqa: BLE001
            ledger = {}
    recs: list[dict] = []
    for v in verdicts:
        prev = ledger.get(v.slug) or {}
        if prev.get("fingerprint") == v.fingerprint:
            out["skipped"].append({"slug": v.slug, "why": f"already re-queued for this content "
                                                          f"({prev.get('rec_id')}, {prev.get('queued_at')})"})
            continue
        attempt = int(prev.get("attempts") or 0) + 1
        if attempt > st.requeue_max_attempts:
            out["exhausted"].append({"slug": v.slug, "attempts": attempt - 1, "failed": v.failed})
            continue
        rid = f"pubgate-{len(recs) + 1:03d}"
        recs.append(build_requeue_rec(rid, v, st, attempt=attempt, unpublished=v.slug in unpublished,
                                      checked_at=now.isoformat(timespec="seconds")))
    out["queued"] = [{"slug": r["article_slug"], "rec_id": r["id"], "attempt": r["attempt"]} for r in recs]
    if not recs or not commit:
        return out
    if storage is None:
        raise GateConfigError("no storage backend: failing rows could not be re-queued")
    storage.write_json(f"agents/{st.requeue_agent_id}/runs/{run_ts}/recommendations.json", {
        "schema_version": "1", "agent_id": st.requeue_agent_id, "site": st.site, "run_ts": run_ts,
        "mode": "article-refresh", "summary": f"{len(recs)} article(s) held by the publish gate.",
        "recommendations": recs,
    })
    out["request_id"] = queue_recs(source_agent=st.requeue_agent_id, site=st.site, run_ts=run_ts,
                                   rec_ids=[r["id"] for r in recs], subject_tag=st.requeue_subject_tag,
                                   source="article-publish-gate", storage=storage)
    stamp = now.isoformat(timespec="seconds")
    for r in recs:
        ledger[r["article_slug"]] = {"fingerprint": r["publish_gate"]["fingerprint"], "rec_id": r["id"],
                                     "run_ts": run_ts, "queued_at": stamp, "attempts": r["attempt"]}
    storage.write_json(ledger_key, ledger)
    return out


def clear_ledger(slugs: Iterable[str], st: GateSettings, storage) -> None:
    """Forget the attempts of rows that were published."""
    slugs = set(slugs)
    if not slugs or storage is None:
        return
    key = f"agents/{st.requeue_agent_id}/state/requeued.json"
    try:
        ledger = storage.read_json(key) or {}
        if any(s in ledger for s in slugs):
            storage.write_json(key, {k: v for k, v in ledger.items() if k not in slugs})
    except Exception:  # noqa: BLE001 - the ledger is bookkeeping, never a blocker
        pass


# ── the run ─────────────────────────────────────────────────────────────────

def build_context(site_hint: str, *, storage, cp_policy: content_policy.ContentPolicy,
                  slug_ok: Callable[[str], bool] | None = None,
                  link_cfg: Mapping[str, Any] | None = None) -> GateContext:
    """The guards' configs for one site: the link guard's resolved block
    (repo default + storage copy), the claims guard's config (storage copy
    or repo default) and the compiled content policy."""
    return GateContext(site_hint=site_hint,
                       link_cfg=dict(link_cfg) if link_cfg is not None else link_guard.resolve_minima(site_hint, storage),
                       claims_cfg=claims_guard.load_config(storage), policy=cp_policy, slug_ok=slug_ok)


def run(args: argparse.Namespace) -> tuple[int, dict]:
    storage = None if args.no_storage else _storage()
    report: dict = {"site": args.site, "committed": bool(args.commit), "enforce": bool(args.enforce),
                    "drafts": [], "published": [], "promoted": [], "held": [], "unpublished": [],
                    "requeue": {}, "errors": []}
    try:
        cp_cfg = content_policy.load_config(storage, path=args.config)
        policy = content_policy.resolve_policy(args.site, config=cp_cfg)
        st = resolve_gate_settings(args.site, cp_cfg, policy)
    except (content_policy.ContentPolicyConfigError, GateConfigError) as e:
        report["errors"].append(f"config: {e}")
        return EXIT_CONFIG, report
    report["site_key"], report["rule_sets"] = st.site, list(policy.rule_sets)
    report["draft_status"] = st.draft_status
    if not st.enabled:
        report["note"] = (f"site {args.site!r} has not opted into a content policy / publish gate; "
                          f"nothing selected, nothing changed")
        return EXIT_OK, report
    since = st.window_hours if args.since_hours is None else float(args.since_hours)
    report["since_hours"] = since
    drafts_since = st.drafts_window_hours if args.drafts_since_hours is None else float(args.drafts_since_hours)
    report["drafts_since_hours"] = drafts_since
    names = author_dsn_candidates(st, args.dsn_env or "", writes=bool(args.commit))
    env_name, dsn = first_env(names)
    if not dsn:
        why = (" (a --commit run never falls back to the generic DATABASE_URL: set DATABASE_URL_"
               f"{st.site_env}, or name a variable with --dsn-env)" if args.commit else "")
        report["errors"].append(f"no author DSN in ${' / $'.join(names)}{why}")
        return EXIT_CONFIG, report
    report["author_dsn_env"] = env_name
    # The read-only author session holds the site's advisory lock for the
    # whole run (selection, promotion, unpublishing, re-queue).
    try:
        store = _open_store(dsn, True, st)
    except Exception as e:  # noqa: BLE001 - nothing is promoted without a session
        report["errors"].append(f"author database unreachable ({env_name}): {_db_error(e)}")
        return EXIT_CONFIG, report
    try:
        return _run_locked(args, st, policy, storage, store, dsn, since, drafts_since, report)
    except GateConfigError as e:
        report["errors"].append(f"config: {e}")
        return EXIT_CONFIG, report
    except Exception as e:  # noqa: BLE001 - a failed query publishes nothing
        report["errors"].append(f"database error: {_db_error(e)}")
        return EXIT_CONFIG, report
    finally:
        store.close()


_DSN_IN_TEXT = re.compile(r"postgres(?:ql)?://\S+")


def _db_error(e: BaseException) -> str:
    """A database error for the report: type and first line, never a DSN."""
    first = (str(e).strip().splitlines() or [""])[0]
    return f"{type(e).__name__}: {_DSN_IN_TEXT.sub('postgresql://***', first)[:200]}"


def _apply_each(items: Sequence[RowVerdict], fn: Callable[[RowVerdict], str], conn) -> None:
    """Run `fn` per row; an exception leaves that row unchanged (rolled back)."""
    for v in items:
        try:
            v.note = fn(v)
        except Exception as e:  # noqa: BLE001 - this row stays as it was
            v.note = f"error: {_db_error(e)}"
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass


def _promote_all(promote: Sequence[RowVerdict], rows: Mapping[tuple, dict], st: GateSettings,
                 args: argparse.Namespace, report: dict, author_db: str = "") -> int:
    """Publish passing drafts through the publisher role. Any failure leaves
    the row a draft and returns EXIT_CONFIG."""
    names = publisher_dsn_candidates(st, args.publisher_dsn_env or "")
    pname, pdsn = first_env(names)

    def hold_all(note: str) -> int:
        for v in promote:
            v.action, v.note = "hold", note
        return EXIT_CONFIG

    if not pdsn:
        report["errors"].append(f"no publisher DSN in ${' / $'.join(names)}: {len(promote)} passing draft(s) "
                                f"stay drafts")
        return hold_all("no publisher DSN")
    report["publisher_dsn_env"] = pname
    try:
        pstore = _open_store(pdsn, False, st)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"publisher database unreachable ({pname}): {_db_error(e)}")
        return hold_all("publisher database unreachable")
    try:
        who = pstore.current_user()
        report["publisher_role"] = who
        if st.publisher_role and who != st.publisher_role:
            report["errors"].append(f"publisher DSN logs in as {who!r}, not the configured publisher role "
                                    f"{st.publisher_role!r}: nothing promoted")
            return hold_all("publisher role mismatch")
        pdb = pstore.current_database()
        if (author_db and pdb != author_db) or (st.database_name and pdb != st.database_name):
            report["errors"].append(f"publisher DSN ({pname}) is connected to database {pdb!r}, not the site's "
                                    f"{st.database_name or author_db!r}: nothing promoted")
            return hold_all("publisher database mismatch")
        _apply_each(promote, lambda v: pstore.promote(rows[(v.slug, st.draft_status)]), pstore.conn)
    finally:
        pstore.close()
    code = EXIT_OK
    for v in promote:
        v.action = "promoted" if v.note == "published" else "not-promoted"
        if v.note == "trigger-kept-draft":
            code = EXIT_CONFIG
            report["errors"].append(f"{v.slug}: the database kept the row a draft; {pname} is not the "
                                    f"publisher role")
    return code


def _unpublish_all(unpub: Sequence[RowVerdict], rows: Mapping[tuple, dict], st: GateSettings, dsn: str,
                   report: dict, author_db: str = "") -> int:
    """Return violating published rows to the gate's queue (the author role
    may: leaving the published state is always allowed)."""
    try:
        wstore = _open_store(dsn, False, st)
    except Exception as e:  # noqa: BLE001
        report["errors"].append(f"author database unreachable for unpublishing: {_db_error(e)}")
        for v in unpub:
            v.action, v.note = "not-unpublished", "database unreachable"
        return EXIT_CONFIG
    try:
        wdb = wstore.current_database()
        if author_db and wdb != author_db:
            report["errors"].append(f"the write connection reached database {wdb!r}, not {author_db!r}: "
                                    f"nothing unpublished")
            for v in unpub:
                v.action, v.note = "not-unpublished", "database mismatch"
            return EXIT_CONFIG
        _apply_each(unpub, lambda v: wstore.unpublish(rows[(v.slug, st.published_status)]), wstore.conn)
    finally:
        wstore.close()
    for v in unpub:
        v.action = "unpublished" if v.note == "unpublished" else "not-unpublished"
    return EXIT_OK


def _verify_database(store: ArticleStore, st: GateSettings, report: dict) -> str:
    """'' when the author connection is the site's gated database, else why
    not: publish_gate.database_name when configured, and the publish guard
    installed, enabled and written for this gate's statuses."""
    db = store.current_database()
    report["database"] = db
    if st.database_name and db != st.database_name:
        return (f"the author DSN is connected to database {db!r}, not publish_gate.database_name "
                f"{st.database_name!r}")
    if not st.verify_guard:
        return ""
    g = store.guard_status()
    report["guard"] = g
    fn = g.get("function") or st.guard_function or "a publish guard"
    if not g.get("installed"):
        return (f"{st.table} in database {db!r} has no publish guard trigger ({fn}()): this is not a gated "
                f"site database (render the kit's publish-trigger template, K4, or set "
                f"publish_gate.verify_guard=false only if another guard enforces the same rules)")
    if not g.get("enabled"):
        return f"the publish guard trigger ({fn}()) on {st.table} in database {db!r} is disabled"
    if not g.get("statuses"):
        return (f"the publish guard ({fn}()) in database {db!r} is not written for draft status "
                f"{st.draft_status!r} and published status {st.published_status!r}: render the kit template "
                f"with draftStatus '{st.draft_status}', or set publish_gate.draft_status to the status it uses")
    return ""


def _run_locked(args: argparse.Namespace, st: GateSettings, policy: content_policy.ContentPolicy, storage,
                store: ArticleStore, dsn: str, since: float, drafts_since: float,
                report: dict) -> tuple[int, dict]:
    if not store.try_lock():
        report["note"] = "another publish-gate run holds this site's lock; nothing done"
        return EXIT_OK, report
    author_db = ""
    if args.commit:
        # Before anything is written (promotions, unpublishing, re-queued
        # recs naming these slugs): is this the site's own gated database?
        why = _verify_database(store, st, report)
        if why:
            report["errors"].append(f"refusing to write: {why}")
            return EXIT_CONFIG, report
        author_db = report.get("database", "")
    limit = args.limit or st.limit
    # The queue: every pending row (holds such as 'draft' are never selected).
    drafts = store.select(st.draft_status, since_hours=drafts_since, slugs=args.slug, limit=limit)
    published = (store.select(st.published_status, since_hours=since, slugs=args.slug, limit=limit)
                 if args.enforce else [])
    link_cfg = link_guard.resolve_minima(args.site, storage)
    slug_ok = None
    bodies = [r.get("body_md") or "" for r in drafts + published]
    if bodies and (link_cfg.get("kitchen_slug_check") or {}).get("product_table"):
        try:
            slug_ok = link_guard.product_slug_checker_from_db(store.conn, bodies, link_cfg)
        except Exception as e:  # noqa: BLE001 - counted by shape, as the link-guard sweep does
            report["errors"].append(f"catalog check skipped: {type(e).__name__}: {str(e)[:160]}")
    ctx = build_context(args.site, storage=storage, cp_policy=policy, slug_ok=slug_ok, link_cfg=link_cfg)
    draft_v = [evaluate_row(r, ctx) for r in drafts]
    pub_v = [evaluate_row(r, ctx) for r in published]
    rows_by_slug = {(r.get("slug"), r.get("status")): r for r in drafts + published}

    exit_code = EXIT_OK
    for v in draft_v:
        v.action = "promote" if v.passes else "hold"
    for v in pub_v:
        v.action = "unpublish" if v.fails_any(st.enforce_checks) else ("keep" if v.passes else "keep-warn")
    promote = [v for v in draft_v if v.passes]
    hold = [v for v in draft_v if not v.passes]
    unpub = [v for v in pub_v if v.action == "unpublish"]

    if args.commit and promote:
        exit_code = max(exit_code, _promote_all(promote, rows_by_slug, st, args, report, author_db))
    if args.commit and unpub:
        exit_code = max(exit_code, _unpublish_all(unpub, rows_by_slug, st, dsn, report, author_db))

    to_requeue = hold + [v for v in unpub if (v.action == "unpublished" or not args.commit)]
    if not args.no_requeue:
        try:
            report["requeue"] = requeue(to_requeue, st, storage, commit=bool(args.commit),
                                        unpublished=[v.slug for v in unpub])
        except GateConfigError as e:
            report["errors"].append(str(e))
            exit_code = EXIT_CONFIG
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"re-queue failed: {type(e).__name__}: {str(e)[:200]}")
            exit_code = EXIT_CONFIG
    if args.commit:
        clear_ledger([v.slug for v in draft_v if v.action == "promoted"], st, storage)

    report["drafts"] = [v.to_dict() for v in draft_v]
    report["published"] = [v.to_dict() for v in pub_v]
    report["promoted"] = [v.slug for v in draft_v if v.action == "promoted"]
    report["would_promote"] = [] if args.commit else [v.slug for v in draft_v if v.passes]
    # held: drafts that fail a check, plus passing drafts a commit could not
    # publish (no publisher DSN / role, edited after the check)
    report["held"] = [v.slug for v in draft_v if not v.passes or (args.commit and v.action != "promoted")]
    report["unpublished"] = [v.slug for v in pub_v if v.action == "unpublished"]
    if exit_code == EXIT_OK and args.fail_on_reject and (hold or unpub):
        exit_code = EXIT_FAILED
    return exit_code, report


def _print(report: dict, *, as_json: bool) -> None:
    rows = report.get("drafts", []) + report.get("published", [])
    if as_json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False, default=str))
    else:
        for r in rows:
            mark = "PASS" if r["passes"] else "FAIL " + ",".join(r["failed"])
            reasons = "; ".join(f"{c['name']}: {c.get('reason', '')[:160]}" for c in r["checks"] if not c["passes"])
            print(f"{r['slug']} [{r['status']}] {r['action'] or '-'}: {mark}"
                  + (f" — {reasons}" if reasons else "") + (f" ({r['note']})" if r.get("note") else ""))
    rq = report.get("requeue") or {}
    for q in rq.get("queued", []):
        print(f"re-queue {q['slug']} as {q['rec_id']} (attempt {q['attempt']})"
              + ("" if report.get("committed") else " [dry run]"))
    for q in rq.get("skipped", []):
        print(f"re-queue skipped {q['slug']}: {q['why']}")
    for q in rq.get("exhausted", []):
        print(f"re-queue EXHAUSTED {q['slug']} after {q['attempts']} attempt(s): needs an operator")
    for e in report.get("errors", []):
        print(f"[publish-gate] ERROR {e}", file=sys.stderr)
    if report.get("note"):
        print(f"[publish-gate] {report['note']}", file=sys.stderr)
    if "drafts" in report and report.get("rule_sets") is not None and not report.get("note"):
        promoted = (f"{len(report['promoted'])} promoted" if report.get("committed")
                    else f"{len(report.get('would_promote') or [])} would be promoted")
        print(f"[publish-gate] {report.get('site_key') or report.get('site')}: {len(report['drafts'])} "
              f"{report.get('draft_status', 'draft')} row(s), "
              f"{promoted}, {len(report['held'])} held; "
              f"{len(report['published'])} published row(s) checked, {len(report['unpublished'])} unpublished"
              + ("" if report.get("committed") else " [dry run: nothing written]"), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", required=True, help="site hint (agent id or site name; matches config keys)")
    ap.add_argument("--since-hours", type=float, default=None,
                    help="--enforce: published rows written in the last N hours (default: "
                         "publish_gate.window_hours; 0 = every published row)")
    ap.add_argument("--drafts-since-hours", type=float, default=None,
                    help="only pending rows written in the last N hours (default: publish_gate.drafts_window_hours; "
                         "0 = every pending row, the gate's queue)")
    ap.add_argument("--slug", action="append", default=[], help="only these slugs (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="max rows per status (default: config limit)")
    ap.add_argument("--commit", action="store_true", help="promote / unpublish / re-queue (default: dry run)")
    ap.add_argument("--enforce", action="store_true",
                    help="also check published rows; with --commit unpublish those breaking an enforced check")
    ap.add_argument("--no-requeue", action="store_true", help="never re-queue failing rows")
    ap.add_argument("--dsn-env", default="", help="env var NAME holding the author DSN (the only way a --commit "
                                                   "run uses the generic DATABASE_URL)")
    ap.add_argument("--publisher-dsn-env", default="", help="env var NAME holding the publisher-role DSN")
    ap.add_argument("--config", help="extra content-policy JSON laid over the repo default and storage copy")
    ap.add_argument("--no-storage", action="store_true",
                    help="repo-default configs only (re-queue then has no storage and is reported)")
    ap.add_argument("--json", action="store_true", help="one JSON object per row")
    ap.add_argument("--report-json", help="write the full report here")
    ap.add_argument("--fail-on-reject", action="store_true", help="exit 1 when a row fails")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    code, report = run(args)
    _print(report, as_json=args.json)
    if args.report_json:
        try:
            p = pathlib.Path(args.report_json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        except OSError as e:
            print(f"[publish-gate] report not written: {e}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
