"""Database adapter abstraction.

Supports two backends:
  - Postgres (psycopg2)
  - Azure SQL (pyodbc with the Microsoft ODBC Driver 18 for SQL Server)

Each adapter exposes the same surface:
    introspect_table(table)  → list[ColumnInfo]
    sample_rows(table, n)    → list[dict] (one example row per existing column type)
    upsert_rows(table, rows, key_columns) → InsertResult

Why this abstraction: the agent needs to (1) read the destination's product
schema before proposing a mapping, and (2) write rows after the mapping is
approved. Both ops differ between Postgres and Azure SQL (placeholder
syntax, ON CONFLICT vs MERGE), so a thin adapter layer keeps the agent code
itself backend-agnostic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger("ebay-sync.db")


@dataclass
class ColumnInfo:
    name: str
    data_type: str       # canonical: text|varchar|integer|numeric|boolean|timestamp|jsonb|...
    nullable: bool
    max_length: int | None = None
    is_unique: bool = False
    default: str | None = None


@dataclass
class InsertResult:
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    failed_samples: list[dict] = None


class DbAdapter:
    """Base class — concrete subclasses implement the three methods."""

    kind: str = ""  # 'postgres' | 'azure-sql'

    def __init__(self, dsn: str):
        self.dsn = dsn

    def introspect_table(self, table: str) -> list[ColumnInfo]:
        raise NotImplementedError

    def sample_rows(self, table: str, n: int = 3) -> list[dict]:
        raise NotImplementedError

    def upsert_rows(self, table: str, rows: list[dict], key_columns: list[str]) -> InsertResult:
        raise NotImplementedError

    def close(self):
        pass


# ───────────────────────────────────────────────────────────────────
# Postgres
# ───────────────────────────────────────────────────────────────────

class PostgresAdapter(DbAdapter):
    kind = "postgres"

    def __init__(self, dsn: str):
        super().__init__(dsn)
        import psycopg2
        import psycopg2.extras
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False

    def ensure_open(self) -> None:
        """Reconnect if Postgres dropped the connection (idle timeout, server
        restart, etc). Long-running agents that intersperse heavy non-DB
        work (LLM calls) with DB writes hit Azure Postgres' default idle
        timeout (~5min) and the conn dies silently — every subsequent
        statement raises 'connection already closed'.

        Call this before each batch of writes."""
        try:
            if getattr(self.conn, "closed", 0):
                self.conn = self._psycopg2.connect(self.dsn)
                self.conn.autocommit = False
                return
            # Cheap probe — `SELECT 1` round-trips fast on a healthy conn,
            # raises on a half-closed one (Azure server-side close that
            # python hasn't noticed yet).
            cur = self.conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
        except Exception:
            # Force a fresh connection on any probe failure.
            try: self.conn.close()
            except Exception: pass
            self.conn = self._psycopg2.connect(self.dsn)
            self.conn.autocommit = False

    def introspect_table(self, table: str) -> list[ColumnInfo]:
        # Allow schema-qualified table names ("public.products")
        schema = "public"
        if "." in table:
            schema, table = table.split(".", 1)
        cur = self.conn.cursor()
        cur.execute("""
            SELECT column_name, data_type, is_nullable, character_maximum_length, column_default
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
             ORDER BY ordinal_position
        """, (schema, table))
        cols = []
        for r in cur.fetchall():
            cols.append(ColumnInfo(
                name=r[0],
                data_type=str(r[1]).lower(),
                nullable=str(r[2]).upper() == "YES",
                max_length=r[3],
                default=r[4],
            ))
        # Mark uniques
        cur.execute("""
            SELECT a.attname
              FROM pg_index i
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
             WHERE i.indrelid = %s::regclass AND (i.indisunique OR i.indisprimary)
        """, (f"{schema}.{table}",))
        uniques = {r[0] for r in cur.fetchall()}
        for c in cols:
            if c.name in uniques:
                c.is_unique = True
        cur.close()
        return cols

    def sample_rows(self, table: str, n: int = 3) -> list[dict]:
        cur = self.conn.cursor(cursor_factory=self._extras.RealDictCursor)
        cur.execute(f"SELECT * FROM {table} LIMIT %s", (n,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    def upsert_rows(self, table: str, rows: list[dict], key_columns: list[str]) -> InsertResult:
        if not rows:
            return InsertResult()
        # Probe + reconnect — long Claude-hydration calls upstream can outlive
        # Azure Postgres' 5-min idle timeout, dropping the conn silently.
        self.ensure_open()
        cols = list(rows[0].keys())
        placeholders = ",".join(["%s"] * len(cols))
        col_list = ",".join(cols)
        # Update set excludes key columns; default to update all non-key cols.
        update_cols = [c for c in cols if c not in key_columns and c not in ("created_at",)]
        update_set = ",".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({','.join(key_columns)}) DO UPDATE SET {update_set} "
            f"RETURNING (xmax = 0) AS inserted"
        )
        cur = self.conn.cursor()
        result = InsertResult()
        result.failed_samples = []
        for row in rows:
            try:
                cur.execute(sql, [row[c] for c in cols])
                r = cur.fetchone()
                if r and r[0]:
                    result.inserted += 1
                else:
                    result.updated += 1
            except Exception as e:
                self.conn.rollback()
                result.failed += 1
                if len(result.failed_samples) < 5:
                    result.failed_samples.append({"err": str(e)[:200], "row_keys": list(row.keys())})
                continue
        self.conn.commit()
        cur.close()
        return result

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────
# Azure SQL
# ───────────────────────────────────────────────────────────────────

class AzureSqlAdapter(DbAdapter):
    kind = "azure-sql"

    def __init__(self, dsn: str):
        super().__init__(dsn)
        try:
            import pyodbc  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "pyodbc not installed. Install: pip install pyodbc, "
                "and the Microsoft ODBC Driver 18 for SQL Server."
            ) from e
        self._pyodbc = pyodbc
        self.conn = pyodbc.connect(dsn, autocommit=False)

    def introspect_table(self, table: str) -> list[ColumnInfo]:
        schema = "dbo"
        if "." in table:
            schema, table = table.split(".", 1)
        cur = self.conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH, COLUMN_DEFAULT
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
             ORDER BY ORDINAL_POSITION
        """, (schema, table))
        cols = [
            ColumnInfo(
                name=r[0],
                data_type=str(r[1]).lower(),
                nullable=str(r[2]).upper() == "YES",
                max_length=r[3],
                default=r[4],
            )
            for r in cur.fetchall()
        ]
        # Find primary key + unique constraints
        cur.execute("""
            SELECT k.COLUMN_NAME
              FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
              JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON t.CONSTRAINT_NAME = k.CONSTRAINT_NAME
             WHERE t.TABLE_SCHEMA = ? AND t.TABLE_NAME = ?
               AND t.CONSTRAINT_TYPE IN ('PRIMARY KEY','UNIQUE')
        """, (schema, table))
        uniques = {r[0] for r in cur.fetchall()}
        for c in cols:
            if c.name in uniques:
                c.is_unique = True
        cur.close()
        return cols

    def sample_rows(self, table: str, n: int = 3) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT TOP ({n}) * FROM {table}")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        return rows

    def upsert_rows(self, table: str, rows: list[dict], key_columns: list[str]) -> InsertResult:
        if not rows:
            return InsertResult()
        cols = list(rows[0].keys())
        result = InsertResult()
        result.failed_samples = []
        # Use MERGE per row (cheap to issue; safe under autocommit=False).
        non_key = [c for c in cols if c not in key_columns and c not in ("created_at",)]
        placeholders = ",".join(["?"] * len(cols))
        select_cols = ",".join(f"? AS {c}" for c in cols)
        on_clause = " AND ".join(f"target.{c} = source.{c}" for c in key_columns)
        update_set = ", ".join(f"target.{c} = source.{c}" for c in non_key)
        insert_cols = ",".join(cols)
        insert_vals = ",".join(f"source.{c}" for c in cols)
        sql = (
            f"MERGE {table} AS target USING (SELECT {select_cols}) AS source "
            f"ON ({on_clause}) "
            f"WHEN MATCHED THEN UPDATE SET {update_set} "
            f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals}) "
            f"OUTPUT $action;"
        )
        cur = self.conn.cursor()
        for row in rows:
            try:
                cur.execute(sql, [row[c] for c in cols])
                action_row = cur.fetchone()
                if action_row and action_row[0] == "INSERT":
                    result.inserted += 1
                else:
                    result.updated += 1
            except Exception as e:
                self.conn.rollback()
                result.failed += 1
                if len(result.failed_samples) < 5:
                    result.failed_samples.append({"err": str(e)[:200], "row_keys": list(row.keys())})
                continue
        self.conn.commit()
        cur.close()
        return result

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────
# Factory
# ───────────────────────────────────────────────────────────────────

def make_adapter(kind: str, dsn: str) -> DbAdapter:
    k = (kind or "").lower()
    if k in ("postgres", "postgresql", "pg"):
        return PostgresAdapter(dsn)
    if k in ("azure-sql", "mssql", "sqlserver", "azure_sql"):
        return AzureSqlAdapter(dsn)
    raise ValueError(f"Unsupported db kind: {kind!r}. Use 'postgres' or 'azure-sql'.")
