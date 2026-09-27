"""framework.core.pg_conn: reconnect after an idle-timeout close (no live DB needed)."""
from framework.core import pg_conn


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        if self.conn.dead:
            raise Exception("server closed the connection unexpectedly")
        self.conn.executed.append(sql)
        self.conn.status = 2  # a SELECT opens a transaction

    def fetchone(self):
        return (1,)


class _Conn:
    def __init__(self, closed=0, dead=False, status=0):
        self.closed, self.dead, self.status = closed, dead, status
        self.executed, self.rolled_back, self.close_calls, self.autocommit = [], 0, 0, False

    def get_transaction_status(self):
        return self.status

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        self.rolled_back += 1
        self.status = 0

    def close(self):
        self.close_calls += 1
        self.closed = 1


def test_live_connection_is_reused_and_left_idle(monkeypatch):
    monkeypatch.setattr(pg_conn, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no reconnect")))
    c = _Conn()
    assert pg_conn.ensure_open(c, "dsn") is c
    assert c.rolled_back == 1 and c.status == 0  # the probe did not leave a transaction open


def test_open_transaction_is_not_rolled_back(monkeypatch):
    monkeypatch.setattr(pg_conn, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no reconnect")))
    c = _Conn(status=2)  # pending writes
    assert pg_conn.ensure_open(c, "dsn") is c and c.rolled_back == 0


def test_closed_or_dead_connection_is_replaced(monkeypatch):
    fresh = _Conn()
    seen = {}
    monkeypatch.setattr(pg_conn, "connect", lambda dsn, **kw: seen.update(dsn=dsn, kw=kw) or fresh)
    for old in (_Conn(closed=1), _Conn(dead=True)):
        assert pg_conn.ensure_open(old, "dsn", cursor_factory="X") is fresh
        assert seen == {"dsn": "dsn", "kw": {"cursor_factory": "X"}}
    assert pg_conn.ensure_open(None, "dsn") is fresh


def test_connect_adds_keepalives(monkeypatch):
    import psycopg2
    got = {}
    monkeypatch.setattr(psycopg2, "connect", lambda dsn, **kw: got.update(kw) or "conn")
    assert pg_conn.connect("dsn", keepalives_idle=30) == "conn"
    assert got["keepalives"] == 1 and got["keepalives_idle"] == 30  # caller wins
