"""Connect failures as text that never carries the DSN.

A Postgres DSN holds a password, and it must never reach a run record,
framework storage, a log or the CLI. A connect failure is where it leaks:
libpq echoes pieces of a malformed connection string in its messages (a
mis-encoded password included), inside quotes, and an unencoded '@' or '/'
in a URI password turns part of the password into the host name or the
port it then reports. :func:`connect_error_text` turns the driver's
exception into text that is safe to store and show:

* a parse error of the DSN itself (``ProgrammingError``, "invalid dsn",
  "connection info string", "URI") keeps no driver text at all, only how to
  fix the DSN;
* any other message loses every quoted segment;
* what is left loses every run of at least 4 characters found in the
  password (:func:`redact_dsn`, which reads the password leniently through
  :func:`password_fragments`, so a malformed DSN is still covered).

Raise your own exception with that text OUTSIDE the ``except`` block, so no
``__context__`` carries the driver's exception (and its echo) along.

The helpers are niche- and site-agnostic. agents/spec-extractor-agent and
agents/pick-builder-agent carry the same functions inline (they predate this
module); their output is identical for the default ``label``.
"""
from __future__ import annotations

import re
import urllib.parse

#: libpq quotes every piece of the connection string it echoes ("..."; other
#: marks under a translated client locale).
_QUOTED_RE = re.compile("\"[^\"]*\"|'[^']*'|“[^”]*”|„[^“”]*[“”]"
                        "|«[^»]*»|‘[^’]*’")
#: A parse error of the connection string itself: its detail IS the DSN.
_INVALID_DSN_RE = re.compile(r"invalid dsn|connection info string|\bURI\b")
_PASSWORD_KW_RE = re.compile(r"password\s*=\s*('[^']*'?|\S+)", re.I)
_URI_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9+.-]*://(.*)$", re.S)
_PIECES_RE = re.compile(r"[\s@:/?#&=,;\"'%]+")
_MIN_FRAGMENT = 4
REDACTED = "<redacted>"


def password_fragments(dsn: str) -> list[str]:
    """What a driver message could echo of the DSN's password, read
    leniently: ``password=...`` (keyword form or URI query) and the URI user
    info up to the LAST '@' (a password holding an unencoded '@', '/', '?' or
    '#' breaks the URI but is still found); raw and percent-decoded, whole
    and in pieces of at least 4 characters, longest first."""
    dsn = str(dsn or "")
    found = {m.group(1).strip("'") for m in _PASSWORD_KW_RE.finditer(dsn)}
    m = _URI_RE.match(dsn)
    if m and "@" in m.group(1):
        userinfo = m.group(1)[:m.group(1).rfind("@")]
        if ":" in userinfo:
            found.add(userinfo.split(":", 1)[1])
    out: set[str] = set()
    for f in found:
        for v in (f, urllib.parse.unquote(f)):
            out.add(v)
            out.update(_PIECES_RE.split(v))
    return sorted((x for x in out if len(x) >= _MIN_FRAGMENT), key=lambda x: (-len(x), x))


def redact_dsn(text: str, dsn: str) -> str:
    """``text`` with every run of at least 4 characters found inside a
    password fragment of ``dsn`` replaced by ``<redacted>``."""
    frags = password_fragments(dsn)
    if not frags or not text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        best = 0
        if i + _MIN_FRAGMENT <= n:
            window = text[i:i + _MIN_FRAGMENT]
            for f in frags:
                if window in f:
                    k = _MIN_FRAGMENT
                    while i + k < n and text[i:i + k + 1] in f:
                        k += 1
                    best = max(best, k)
        if best:
            out.append(REDACTED)
            i += best
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def connect_error_text(exc: BaseException, *, dsn_env: str, dsn: str = "",
                       label: str = "site DB") -> str:
    """A connect failure as text that is safe to store and show:
    ``cannot connect to the <label> ($<dsn_env>): <class>: <detail>``. libpq
    echoes pieces of a malformed connection string (a mis-encoded password
    included) inside quotes, so a parse error of the DSN keeps no driver text
    at all, any other message loses every quoted segment, and what is left
    loses every fragment of the password (:func:`redact_dsn`). ``dsn_env`` is
    the NAME of the variable that held the DSN (omitted when empty)."""
    kind = type(exc).__name__
    raw = " ".join(str(exc).split())
    if kind == "ProgrammingError" or _INVALID_DSN_RE.search(raw):
        detail = ("not a valid connection string or URI (the driver's message is withheld: it "
                  "echoes the DSN). In a URI, percent-encode special characters of the password "
                  "(% as %25, @ as %40, / as %2F, # as %23, ? as %3F, a space as %20).")
    else:
        detail = _QUOTED_RE.sub(f'"{REDACTED}"', raw)
    where = f" (${dsn_env})" if dsn_env else ""
    return f"cannot connect to the {label}{where}: {kind}: {redact_dsn(detail, dsn)[:400]}"
