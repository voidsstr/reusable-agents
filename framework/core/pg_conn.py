"""Postgres connections that survive long gaps between statements.

Why (2026-09-27): agents that interleave slow LLM calls with DB writes lose
their connection to Azure Postgres' idle timeout (~5 min, less behind the Azure
load balancer). Every later statement raises "connection already closed", and a
@with_retry around the write cannot help because it retries with the same dead
connection object. specpicks-product-hydration-agent lost a whole run that way:
80 products hydrated by Opus, 0 written. The eBay sync agent had solved it
locally (db_adapter.PostgresAdapter.ensure_open); this is that fix as a shared
primitive.

  conn = connect(dsn)                      # TCP keepalives on by default
  ...slow LLM work...
  conn = ensure_open(conn, dsn)            # before the next write

Keepalives keep an idle-but-healthy connection from being dropped by the
network path; ensure_open catches whatever still gets through (server-side
idle close, restarts, failover).
"""
from __future__ import annotations

from typing import Any

KEEPALIVE_KW: dict[str, Any] = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 15,
    "keepalives_count": 4,
}


def connect(dsn: str, **kw: Any):
    """psycopg2.connect with TCP keepalives (caller kwargs win)."""
    import psycopg2

    return psycopg2.connect(dsn, **{**KEEPALIVE_KW, **kw})


def is_open(conn) -> bool:
    """True when `conn` answers a trivial query. Leaves no transaction open
    on a connection that was idle (the probe is rolled back only if it started
    the transaction)."""
    if conn is None or getattr(conn, "closed", 1):
        return False
    try:
        idle = conn.get_transaction_status() == 0  # TRANSACTION_STATUS_IDLE
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        if idle and not getattr(conn, "autocommit", False):
            conn.rollback()
        return True
    except Exception:  # noqa: BLE001 - any probe failure means "reconnect"
        return False


def ensure_open(conn, dsn: str, **connect_kw: Any):
    """Return `conn` if it still works, else close it and return a fresh
    connection opened with the same kwargs. Callers must re-bind:
    ``conn = ensure_open(conn, dsn)``."""
    if is_open(conn):
        return conn
    try:
        if conn is not None:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    return connect(dsn, **connect_kw)
