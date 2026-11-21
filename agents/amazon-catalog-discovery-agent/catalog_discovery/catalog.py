"""The site's catalog table, as discovery sees it: dedupe and ASIN-only inserts.

Both statements come from the site's config (``catalog.known_sql`` and
``catalog.insert_sql``), so the engine never assumes a table layout. The
insert binds a fixed set of OUR values (:data:`config.INSERT_PARAMS`): the
ASIN, the lane label and its kind/category, our confidence, provenance and
the run timestamp. No Amazon title, price, image, brand or URL can reach the
statement, because the engine never passes one; config validation rejects a
template that asks for any other parameter.

Each row is inserted inside its own SAVEPOINT, so one bad row (a CHECK
violation, a missing category) is counted and skipped instead of aborting the
batch. In dry-run mode nothing is executed except the read-only dedupe.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from framework.core.dsn_redaction import connect_error_text, redact_dsn

from .config import CatalogSettings, INSERT_PARAMS

KNOWN_CHUNK = 1000


@dataclass(frozen=True)
class CatalogRow:
    """One ASIN to persist, with our own labels only."""

    asin: str
    lane: str
    lane_kind: str
    lane_category: str
    confidence: Optional[float]
    via: str
    seed: Optional[str] = None

    def params(self, *, run_ts: str, site_id: Optional[str]) -> dict[str, Any]:
        out = {"asin": self.asin, "lane": self.lane, "lane_kind": self.lane_kind,
               "lane_category": self.lane_category,
               "confidence": None if self.confidence is None else round(float(self.confidence), 4),
               "via": self.via, "seed": self.seed, "run_ts": run_ts, "site_id": site_id}
        assert set(out) == set(INSERT_PARAMS)  # the documented contract, nothing more
        return out


@dataclass
class InsertOutcome:
    inserted: list[CatalogRow] = field(default_factory=list)
    noop: int = 0            # rowcount 0: already there (ON CONFLICT) or filtered by the SQL
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)

    def merge(self, other: "InsertOutcome") -> None:
        self.inserted.extend(other.inserted)
        self.noop += other.noop
        self.errors += other.errors
        self.error_samples.extend(other.error_samples[: max(0, 5 - len(self.error_samples))])


class Catalog:
    """Thin wrapper over a DB-API connection (psycopg2 in production)."""

    def __init__(self, conn: Any, settings: CatalogSettings, *, run_ts: str,
                 dry_run: bool = False) -> None:
        self.conn = conn
        self.settings = settings
        self.run_ts = run_ts
        self.dry_run = dry_run

    def known(self, asins: Sequence[str]) -> set[str]:
        """The subset of ``asins`` the catalog already carries."""
        wanted = sorted({a for a in asins if a})
        found: set[str] = set()
        for i in range(0, len(wanted), KNOWN_CHUNK):
            chunk = wanted[i:i + KNOWN_CHUNK]
            with self.conn.cursor() as cur:
                cur.execute(self.settings.known_sql, {"asins": chunk})
                found.update(str(r[0]).strip().upper() for r in cur.fetchall() if r and r[0])
        return found & set(wanted)

    def insert(self, rows: Sequence[CatalogRow]) -> InsertOutcome:
        out = InsertOutcome()
        if not rows:
            return out
        if self.dry_run:
            out.inserted.extend(rows)
            return out
        with self.conn.cursor() as cur:
            for row in rows:
                params = row.params(run_ts=self.run_ts, site_id=self.settings.site_id)
                cur.execute("SAVEPOINT catalog_discovery_row")
                try:
                    cur.execute(self.settings.insert_sql, params)
                    count = cur.rowcount
                except Exception as exc:  # noqa: BLE001 -- one bad row never aborts the batch
                    cur.execute("ROLLBACK TO SAVEPOINT catalog_discovery_row")
                    out.errors += 1
                    if len(out.error_samples) < 5:
                        out.error_samples.append(f"{row.asin}: {type(exc).__name__}: {str(exc)[:160]}")
                    continue
                cur.execute("RELEASE SAVEPOINT catalog_discovery_row")
                if count and count > 0:
                    out.inserted.append(row)
                else:
                    out.noop += 1
        return out

    def commit(self) -> None:
        if self.dry_run:
            self.conn.rollback()
        else:
            self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# DSN resolution and connections
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DsnResolution:
    """Which env var supplies a DSN. ``value`` is never logged or stored."""

    env_name: Optional[str]
    value: Optional[str] = field(default=None, repr=False)
    source: str = ""          # 'dsn_env' | 'site-registry' | 'derived'

    @property
    def ok(self) -> bool:
        return bool(self.value)


def resolve_dsn(*, dsn_env: Optional[str], site: Optional[str], storage: Any = None,
                environ: Optional[Mapping[str, str]] = None) -> DsnResolution:
    """An explicit ``dsn_env`` wins; else the site registry's entry for
    ``site`` (framework.core.site_registry, F1a); else the registry's derived
    default name (DATABASE_URL_<SITE>) for a site the registry does not
    define yet."""
    env = os.environ if environ is None else environ
    if dsn_env:
        return DsnResolution(dsn_env, env.get(dsn_env) or None, "dsn_env")
    if not site:
        return DsnResolution(None, None, "")
    from framework.core import site_registry
    try:
        name = site_registry.dsn_env_for(site, storage=storage)
        source = "site-registry"
    except (site_registry.UnknownSiteError, site_registry.SiteRegistryError):
        name = site_registry.default_dsn_env(site)
        source = "derived"
    return DsnResolution(name, env.get(name) or None, source)


class CatalogConnectError(RuntimeError):
    """A connection could not be opened. The text is safe to store and show:
    it never carries the DSN or any piece of its password."""


def connect_failure_text(exc: BaseException, *, dsn_env: str, dsn: str, label: str) -> str:
    """The text of any exception a connect callable raised, safe to store
    (framework.core.dsn_redaction)."""
    if isinstance(exc, CatalogConnectError):
        return redact_dsn(str(exc), dsn)
    return connect_error_text(exc, dsn_env=dsn_env, dsn=dsn, label=label)


def connect(dsn: str, *, application_name: str, statement_timeout_ms: int,
            connect_timeout_s: int = 15, readonly: bool = False, dsn_env: str = "",
            label: str = "") -> Any:
    """A psycopg2 connection with a statement timeout; READ ONLY on request.

    READ ONLY is set twice: ``default_transaction_read_only=on`` for the whole
    SESSION (a transaction started after a COMMIT is read-only too), and the
    psycopg2 session flag. The seed SQL is also one statement by validation
    and runs as a subquery, so it cannot carry a 'COMMIT; DELETE ...' tail.
    The DSN of a sister database should still be a SELECT-only role.

    A failure raises :class:`CatalogConnectError` naming ``label`` (default
    "seed DB" when ``readonly``, else "catalog DB") and ``$dsn_env`` (the
    variable's NAME): libpq echoes a malformed DSN, its password included,
    so the driver's text is withheld or redacted, and the error is raised
    outside the ``except`` block so no ``__context__`` carries it along.
    """
    import psycopg2  # type: ignore  # framework host dependency

    options = f"-c statement_timeout={int(statement_timeout_ms)}"
    if readonly:
        options += " -c default_transaction_read_only=on"
    conn = None
    failure = ""
    try:
        conn = psycopg2.connect(dsn, connect_timeout=int(connect_timeout_s),
                                application_name=application_name[:63], options=options)
        conn.autocommit = False
        if readonly:
            conn.set_session(readonly=True)
    except Exception as exc:  # noqa: BLE001 -- re-raised below without the driver's text
        failure = connect_error_text(exc, dsn_env=dsn_env, dsn=dsn,
                                     label=label or ("seed DB" if readonly else "catalog DB"))
    if failure:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        raise CatalogConnectError(failure)
    return conn

