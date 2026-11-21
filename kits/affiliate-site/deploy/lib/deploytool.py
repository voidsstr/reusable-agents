#!/usr/bin/env python3
"""deploytool: the parsing and checking half of the affiliate-site kit deploy scripts.

The bash scripts in ../azure and ../aws orchestrate cloud CLIs; everything
that needs real parsing or a careful check lives here, stdlib only
(python >= 3.9), so it runs on any fleet host without a virtualenv:

  env-dump          parse a KEY=value env file (deploy.env, secrets.env) without
                    executing it; NUL-separated output keeps values off argv
  dsn-info          user/host/port/dbname of a Postgres DSN (never the password)
  role-verifier     SCRAM-SHA-256 verifier for the password in a role's DSN, so
                    the plaintext never reaches the server or its logs
  new-dsn           generate a role password, append the DSN to a 0600 file,
                    print its verifier
  check-cache-header  the HTML cache rule (s-maxage <= the limit, nothing stale
                    past it, CDN-targeted headers included)
  site-cache        the same limit, checked statically in site.json
  render-dockerfile render (or drift-check) Dockerfile.azure from the template
  smoke             post-deploy HTTP checks, including the HTML cache rule
  json-from-files   build a JSON object of secrets from 0600 files
  ecs-taskdef       ECS Fargate task definition (secrets by ARN reference only)
  scaling-policy    Application Auto Scaling target-tracking policy document
  dns-check         DNS-over-HTTPS check of the records a managed certificate needs
  image-users       which Container Apps / jobs (az ... list JSON) run images
                    from one registry repository

Secret handling (every subcommand): a secret value is read from a file, never
from argv; it is never printed; diagnostics name fields, not values; files
this tool writes that contain secrets are created with mode 0600.

Exit codes: 0 ok; 1 a check failed; 2 usage or input error; 3 a mismatch the
operator has to fix (DNS not ready, DSN for the wrong role, stale render);
4 a remote dependency (the DNS-over-HTTPS resolver) was unreachable.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_MISMATCH = 3
EXIT_UNREACHABLE = 4

#: Marker line every rendered Dockerfile carries; deploy.sh drift-checks files that have it.
RENDER_MARKER = "# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl"


class ToolError(Exception):
    """An error with an exit code and a message that never contains a secret."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# ─── env files ────────────────────────────────────────────────────────────────

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_value(raw: str) -> str:
    """Value part of a KEY=value line, with the quoting a POSIX shell applies.

    Single quotes are literal; double quotes allow backslash escapes of
    `"`, `\\`, `$` and backquote; unquoted text may escape any character.
    Anything a shell would expand or execute ($, backquote, ;, &, |, <, >,
    parentheses, unquoted whitespace before more text) is rejected rather
    than silently taken literally, so the file means the same thing to this
    parser, to bash and to systemd's EnvironmentFile=. Error messages never
    contain the value.
    """
    buf: List[str] = []
    i, n = 0, len(raw)
    if raw.startswith("#"):
        return ""
    while i < n:
        c = raw[i]
        if c == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                raise ValueError("unterminated single quote")
            buf.append(raw[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise ValueError("unterminated double quote")
                c = raw[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n and raw[i + 1] in '"\\$`':
                    buf.append(raw[i + 1])
                    i += 2
                    continue
                if c in "$`":
                    raise ValueError("expansion ($ or backquote) inside double quotes; single-quote the value")
                buf.append(c)
                i += 1
        elif c.isspace():
            rest = raw[i:].strip()
            if rest and not rest.startswith("#"):
                raise ValueError("unquoted whitespace inside the value; quote it")
            break
        elif c == "\\":
            if i + 1 >= n:
                raise ValueError("trailing backslash")
            buf.append(raw[i + 1])
            i += 2
        elif c in "$`":
            raise ValueError("expansion ($ or backquote); single-quote the value")
        elif c in ";&|<>()":
            raise ValueError("shell metacharacter in an unquoted value; quote it")
        else:
            buf.append(c)
            i += 1
    return "".join(buf)


def parse_env_text(text: str, *, strict: bool, keys: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """KEY -> value for a shell-style env file. Later assignments win.

    strict: every non-blank, non-comment line must be a valid assignment
    (deploy.env). Not strict (secrets.env, a file other tools own): lines that
    are not assignments, and assignments of keys not asked for, are skipped
    without being parsed, so an odd line elsewhere never breaks a lookup.
    """
    wanted = set(keys) if keys is not None else None
    out: Dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            if strict:
                raise ToolError(f"line {lineno}: expected KEY=value")
            continue
        key, rest = m.group(1), m.group(2)
        if not strict and wanted is not None and key not in wanted:
            continue
        try:
            value = parse_env_value(rest)
        except ValueError as exc:
            raise ToolError(f"line {lineno} ({key}): {exc}") from None
        if wanted is None or key in wanted:
            out[key] = value
    return out


def cmd_env_dump(args: argparse.Namespace) -> int:
    path = args.file
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ToolError(f"cannot read {path}: {exc.strerror}") from None
    keys = [k for k in (args.keys or "").split(",") if k] or None
    try:
        values = parse_env_text(text, strict=args.strict, keys=keys)
    except ToolError as exc:
        raise ToolError(f"{path}: {exc}", exc.code) from None
    if args.check:
        return EXIT_OK
    out = sys.stdout.buffer
    for key, value in values.items():
        if "\0" in value:
            raise ToolError(f"{path}: {key} contains a NUL byte")
        out.write(key.encode("utf-8") + b"\0" + value.encode("utf-8") + b"\0")
    out.flush()
    return EXIT_OK


# ─── DSNs and SCRAM ───────────────────────────────────────────────────────────

@dataclass
class Dsn:
    user: str = ""
    password: Optional[str] = None
    host: str = ""
    port: int = 5432
    dbname: str = ""
    params: Dict[str, str] = field(default_factory=dict)

    def describe(self) -> Dict[str, object]:
        """Everything except the password."""
        return {"user": self.user, "host": self.host, "port": self.port, "dbname": self.dbname,
                "sslmode": self.params.get("sslmode", ""), "has_password": bool(self.password)}


def _parse_conninfo(text: str) -> Dict[str, str]:
    """libpq key=value conninfo: values may be single-quoted with \\' and \\\\ escapes."""
    out: Dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        m = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*").match(text, i)
        if not m:
            raise ToolError("DSN is neither a postgres:// URL nor key=value conninfo")
        key = m.group(1)
        i = m.end()
        buf: List[str] = []
        if i < n and text[i] == "'":
            i += 1
            while True:
                if i >= n:
                    raise ToolError("DSN has an unterminated quoted value")
                c = text[i]
                if c == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                buf.append(c)
                i += 1
        else:
            while i < n and not text[i].isspace():
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
        out[key] = "".join(buf)
    return out


def parse_dsn(text: str) -> Dsn:
    text = text.strip()
    if not text:
        raise ToolError("DSN is empty")
    if re.match(r"^postgres(?:ql)?://", text):
        try:
            u = urllib.parse.urlsplit(text)
            port = u.port or 5432
        except ValueError:
            raise ToolError("DSN URL is malformed (port?)") from None
        params = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        dsn = Dsn(
            user=urllib.parse.unquote(u.username or ""),
            password=urllib.parse.unquote(u.password) if u.password is not None else None,
            host=(u.hostname or ""),
            port=port,
            dbname=urllib.parse.unquote(u.path.lstrip("/")),
            params=params,
        )
        # libpq lets query parameters supply (and override) the connection fields.
        if params.get("user"):
            dsn.user = params["user"]
        if "password" in params:
            dsn.password = params["password"]
        if params.get("host"):
            dsn.host = params["host"].lower()
        if params.get("dbname"):
            dsn.dbname = params["dbname"]
        if params.get("port"):
            try:
                dsn.port = int(params["port"])
            except ValueError:
                raise ToolError("DSN port is not a number") from None
        return dsn
    kv = _parse_conninfo(text)
    try:
        port = int(kv.get("port", "5432") or "5432")
    except ValueError:
        raise ToolError("DSN port is not a number") from None
    return Dsn(user=kv.get("user", ""), password=kv.get("password"), host=kv.get("host", "").lower(),
               port=port, dbname=kv.get("dbname", ""),
               params={k: v for k, v in kv.items() if k not in ("user", "password", "host", "port", "dbname")})


def read_secret_file(path: str) -> str:
    """A secret from a file, or from stdin when path is '-' (a bash here-string, never argv)."""
    if path == "-":
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        return value
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read()
    except OSError as exc:
        raise ToolError(f"cannot read secret file {path}: {exc.strerror}") from None
    if value.endswith("\n"):
        value = value[:-1]
    return value


def validate_scram_password(password: Optional[str]) -> str:
    if not password:
        raise ToolError("the DSN has no password")
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in password):
        # SASLprep is the identity on printable ASCII; anything else would need it.
        raise ToolError("the password must be printable ASCII without spaces (SASLprep is not implemented)")
    if len(password) < 16:
        raise ToolError("the password is shorter than 16 characters")
    return password


def scram_sha256_verifier(password: str, salt: Optional[bytes] = None, iterations: int = 4096) -> str:
    """The verifier Postgres stores for SCRAM-SHA-256 (RFC 5802 / 7677), as
    `SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`. `ALTER ROLE ...
    PASSWORD '<verifier>'` stores it as is, so the plaintext never leaves this host."""
    if salt is None:
        salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def cmd_dsn_info(args: argparse.Namespace) -> int:
    dsn = parse_dsn(read_secret_file(args.dsn_file))
    label = args.label or "DSN"
    if args.expect_user and dsn.user != args.expect_user:
        raise ToolError(f"{label}: the DSN logs in as {dsn.user or '<no user>'!r}, expected role {args.expect_user!r}",
                        EXIT_MISMATCH)
    if args.expect_db and dsn.dbname != args.expect_db:
        raise ToolError(f"{label}: the DSN names database {dsn.dbname or '<none>'!r}, expected {args.expect_db!r}",
                        EXIT_MISMATCH)
    print(json.dumps(dsn.describe(), sort_keys=True))
    return EXIT_OK


def cmd_role_verifier(args: argparse.Namespace) -> int:
    dsn = parse_dsn(read_secret_file(args.dsn_file))
    label = args.label or "DSN"
    if args.expect_user and dsn.user != args.expect_user:
        raise ToolError(f"{label}: the DSN logs in as {dsn.user or '<no user>'!r}, expected role {args.expect_user!r}",
                        EXIT_MISMATCH)
    if args.expect_db and dsn.dbname != args.expect_db:
        raise ToolError(f"{label}: the DSN names database {dsn.dbname or '<none>'!r}, expected {args.expect_db!r}",
                        EXIT_MISMATCH)
    if args.expect_host and dsn.host and dsn.host != args.expect_host.lower():
        print(f"WARN: {label}: the DSN points at host {dsn.host!r}, not {args.expect_host!r}", file=sys.stderr)
    if args.expect_port and dsn.port != args.expect_port:
        print(f"WARN: {label}: the DSN uses port {dsn.port}, not {args.expect_port}", file=sys.stderr)
    try:
        password = validate_scram_password(dsn.password)
    except ToolError as exc:
        raise ToolError(f"{label}: {exc}", exc.code) from None
    salt = base64.b64decode(args.salt_b64) if args.salt_b64 else None
    print(scram_sha256_verifier(password, salt=salt, iterations=args.iterations))
    return EXIT_OK


_ALNUM = string.ascii_letters + string.digits


def cmd_new_dsn(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.var):
        raise ToolError(f"bad variable name {args.var!r}")
    path = os.path.expanduser(args.out)
    existing = ""
    if os.path.exists(path):
        st = os.stat(path)
        if st.st_mode & 0o077:
            raise ToolError(f"{path} is readable by group or others; chmod 600 it first")
        existing = open(path, "r", encoding="utf-8").read()
        if args.var in parse_env_text(existing, strict=False, keys=[args.var]):
            raise ToolError(f"{path} already defines {args.var}; not overwriting it", EXIT_MISMATCH)
    password = "".join(secrets.choice(_ALNUM) for _ in range(32))
    host = args.host if ":" not in args.host else f"[{args.host}]"
    dsn = (f"postgresql://{urllib.parse.quote(args.user, safe='')}:{password}@{host}:{args.port}/"
           f"{urllib.parse.quote(args.db, safe='')}?sslmode={urllib.parse.quote(args.sslmode, safe='')}")
    line = f"{args.var}='{dsn}'\n"
    if existing and not existing.endswith("\n"):
        line = "\n" + line
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    print(scram_sha256_verifier(password))
    return EXIT_OK


# ─── Cache-Control: the rule the price window depends on ──────────────────────

def parse_cache_control(value: str) -> Dict[str, List[str]]:
    """Directive -> every value it was given ('' for a bare directive)."""
    out: Dict[str, List[str]] = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, arg = part.partition("=")
        out.setdefault(name.strip().lower(), []).append(arg.strip().strip('"'))
    return out


#: Targeted cache headers (RFC 9213 and CDN vendors). A CDN that honours one of
#: them uses it instead of Cache-Control, so each must keep the same limit.
TARGETED_CACHE_HEADERS = ("cdn-cache-control", "cloudflare-cdn-cache-control", "surrogate-control")

#: Directives that let a cache serve a stored response after it went stale.
_STALE_DIRECTIVES = ("stale-while-revalidate", "stale-if-error")


def _directive_problems(header: str, value: str, max_shared_age: int, *, need_shared_lifetime: bool) -> List[str]:
    """The limit applied to one Cache-Control-syntax header value. Problems in
    Cache-Control itself are reported bare; those in a targeted header name it."""
    problems: List[str] = []
    at = "" if header == "Cache-Control" else f"{header}: "
    d = parse_cache_control(value)
    if "no-store" in d or "private" in d:
        return problems

    def seconds(name: str) -> Optional[int]:
        vals = d.get(name)
        if vals is None:
            return None
        worst = None
        for v in vals:
            if not re.fullmatch(r"\d+", v):
                problems.append(f"{at}{name}={v!r} is not a number of seconds")
                continue
            worst = max(worst or 0, int(v))
        return worst

    s_maxage = seconds("s-maxage")
    max_age = seconds("max-age")
    if s_maxage is not None and s_maxage > max_shared_age:
        problems.append(f"{at}s-maxage={s_maxage} > {max_shared_age}")
    if max_age is not None and max_age > max_shared_age:
        problems.append(f"{at}max-age={max_age} > {max_shared_age}")
    if s_maxage is None and "no-cache" not in d:
        if need_shared_lifetime:
            problems.append(f"{header} {value!r} has no s-maxage; SSR HTML must send s-maxage <= {max_shared_age}")
        elif max_age is None:
            problems.append(f"{header} {value!r} sets no max-age: a CDN that honours it caches the HTML for a "
                            f"heuristic lifetime, not <= {max_shared_age}s")
    for name in _STALE_DIRECTIVES:
        stale = seconds(name)
        if stale is not None and stale > 0:
            problems.append(f"{at}{name}={stale} lets a shared cache serve the HTML up to {stale}s past "
                            f"its lifetime, beyond the {max_shared_age}s the price window allows")
    return problems


def html_cache_problems(cache_control: Optional[str], age: Optional[str], max_shared_age: int,
                        targeted: Optional[Dict[str, str]] = None) -> List[str]:
    """Why an HTML response breaks the kit's cache rule; [] when it is fine.

    The kit quotes a price only while it is younger than 24h minus the longest
    HTML cache age (site.json cache.htmlSMaxAgeSeconds <= 600). So every SSR
    HTML response must carry s-maxage <= the limit (or not be storable by a
    shared cache at all), max-age must not exceed it either, nothing may let a
    cache serve it stale past that (stale-while-revalidate, stale-if-error),
    a CDN-targeted header (CDN-Cache-Control, Cloudflare-CDN-Cache-Control,
    Surrogate-Control) must keep the same limit, and no shared cache may serve
    the page older than that (Age header).
    """
    problems: List[str] = []
    if age is not None and age.strip():
        try:
            age_s = int(age.strip())
        except ValueError:
            problems.append(f"unparseable Age header {age!r}")
        else:
            if age_s > max_shared_age:
                problems.append(f"Age {age_s}s > {max_shared_age}s: a shared cache is serving this HTML past the limit "
                                f"(a CDN 'cache everything' rule on HTML?)")
    for name, value in sorted((targeted or {}).items()):
        if value is not None and value.strip():
            problems += _directive_problems(name, value, max_shared_age, need_shared_lifetime=False)
    if cache_control is None or not cache_control.strip():
        problems.append(f"no Cache-Control header; SSR HTML must send s-maxage <= {max_shared_age}")
        return problems
    problems += _directive_problems("Cache-Control", cache_control, max_shared_age, need_shared_lifetime=True)
    return problems


def cmd_check_cache_header(args: argparse.Namespace) -> int:
    cc = None if args.missing else args.cache_control
    targeted: Dict[str, str] = {}
    for item in args.targeted or []:
        name, sep, value = item.partition("=")
        if not sep or name.strip().lower() not in TARGETED_CACHE_HEADERS:
            raise ToolError(f"--targeted {item!r} must be HEADER=VALUE with HEADER one of "
                            + ", ".join(TARGETED_CACHE_HEADERS))
        targeted[name.strip().lower()] = value
    problems = html_cache_problems(cc, args.age, args.max, targeted)
    print(json.dumps({"ok": not problems, "problems": problems}))
    return EXIT_OK if not problems else EXIT_FAILED


def cmd_site_cache(args: argparse.Namespace) -> int:
    try:
        with open(args.site_json, "r", encoding="utf-8") as fh:
            site = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ToolError(f"cannot read {args.site_json}: {exc}") from None
    cache = site.get("cache") or {}
    if not isinstance(cache, dict):
        raise ToolError(f"{args.site_json}: cache must be an object")
    values = {
        "htmlSMaxAgeSeconds": cache.get("htmlSMaxAgeSeconds", 600),
        "htmlMaxAgeSeconds": cache.get("htmlMaxAgeSeconds", 60),
        "ssrCacheSeconds": cache.get("ssrCacheSeconds", 0),
    }
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ToolError(f"{args.site_json}: cache.{key} must be a non-negative integer")
    bad = [f"cache.{k}={v} > {args.max}" for k, v in values.items()
           if k in ("htmlSMaxAgeSeconds", "htmlMaxAgeSeconds") and v > args.max]
    if bad:
        raise ToolError(f"{args.site_json}: " + "; ".join(bad) + " (SSR HTML must carry s-maxage <= "
                        f"{args.max}; prices are only quotable for 24h minus the HTML cache age)", EXIT_MISMATCH)
    print(" ".join(f"{k}={v}" for k, v in values.items()))
    return EXIT_OK


# ─── Dockerfile template ──────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_SAFE_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")


def render_template(template: str, values: Dict[str, str]) -> str:
    missing = sorted({m.group(1) for m in _PLACEHOLDER.finditer(template)} - set(values))
    if missing:
        raise ToolError("template placeholders without a value: " + ", ".join(missing))
    out = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
    if "{{" in out:
        raise ToolError("rendered text still contains '{{'")
    return out


def dockerfile_values(args: argparse.Namespace) -> Dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", args.node_image):
        raise ToolError(f"NODE_IMAGE {args.node_image!r} is not an image reference")
    if not (1 <= args.port <= 65535):
        raise ToolError(f"port {args.port} out of range")
    if not _SAFE_PATH.fullmatch(args.server_entry) or ".." in args.server_entry.split("/"):
        raise ToolError(f"SERVER_ENTRY {args.server_entry!r} must be a plain relative path")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]*", args.health_path) or ".." in args.health_path.split("/"):
        raise ToolError(f"HEALTH_PATH {args.health_path!r} must be an absolute URL path like /health")
    if not re.fullmatch(r"[A-Za-z0-9:_-]+", args.build_script):
        raise ToolError(f"BUILD_SCRIPT {args.build_script!r} must be an npm script name")
    build_copy: List[str] = []
    runtime_copy: List[str] = []
    chmod_paths: List[str] = []
    for p in (args.extra_paths or "").split():
        p = p.rstrip("/")
        if not _SAFE_PATH.fullmatch(p) or ".." in p.split("/") or p.startswith("/"):
            raise ToolError(f"DOCKER_EXTRA_PATHS entry {p!r} must be a plain relative path")
        build_copy.append(f"COPY {p} ./{p}")
        runtime_copy.append(f"COPY --from=build /app/{p} ./{p}")
        chmod_paths.append(p)
    none = "# (none: DOCKER_EXTRA_PATHS is empty)"
    return {
        "APP_NAME": args.app_name,
        "KIT_VERSION": args.kit_version,
        "NODE_IMAGE": args.node_image,
        "TARGET_PORT": str(args.port),
        "SERVER_ENTRY": args.server_entry,
        "BUILD_SCRIPT": args.build_script,
        "HEALTH_PATH": args.health_path,
        "EXTRA_BUILD_COPY": "\n".join(build_copy) or none,
        "EXTRA_RUNTIME_COPY": "\n".join(runtime_copy) or none,
        # The runtime stage's copies, made readable by the server's user (after the base four).
        "EXTRA_CHMOD_PATHS": "".join(f" {p}" for p in chmod_paths),
        "RENDER_MARKER": RENDER_MARKER,
    }


def cmd_render_dockerfile(args: argparse.Namespace) -> int:
    with open(args.template, "r", encoding="utf-8") as fh:
        template = fh.read()
    rendered = render_template(template, dockerfile_values(args))
    if RENDER_MARKER not in rendered:
        raise ToolError("the template lost its render marker")
    if args.check:
        try:
            with open(args.check, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError as exc:
            raise ToolError(f"cannot read {args.check}: {exc.strerror}") from None
        if current == rendered:
            print(f"{args.check}: up to date with the kit template")
            return EXIT_OK
        diff = difflib.unified_diff(current.splitlines(True), rendered.splitlines(True),
                                    fromfile=args.check, tofile="rendered from the kit template")
        sys.stderr.write("".join(list(diff)[:200]))
        raise ToolError(f"{args.check} differs from the kit template rendered with this deploy.env; "
                        "re-render it (deploy/render-dockerfile.sh) and commit the result", EXIT_MISMATCH)
    if args.out in (None, "-"):
        sys.stdout.write(rendered)
    else:
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp, args.out)
    return EXIT_OK


# ─── smoke check ──────────────────────────────────────────────────────────────

@dataclass
class PathSpec:
    path: str
    status: int


def parse_path_spec(spec: str) -> PathSpec:
    path, _, status = spec.partition("=")
    if not path.startswith("/") or any(c.isspace() for c in path):
        raise ToolError(f"smoke path {spec!r} must start with / (optionally /path=<status>)")
    if status and not re.fullmatch(r"[1-5]\d\d", status):
        raise ToolError(f"smoke path {spec!r}: expected status must be a 3-digit HTTP code")
    return PathSpec(path, int(status) if status else 200)


class _ConnectToHTTPS(http.client.HTTPSConnection):
    """HTTPS to the URL's host name (SNI, certificate check, Host header) over a
    TCP connection to another address, like curl --connect-to: proves the
    origin itself serves the custom domain with a valid certificate."""

    def __init__(self, host: str, port: int, connect_host: str, connect_port: int, timeout: float,
                 context: ssl.SSLContext) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._connect_host = connect_host
        self._connect_port = connect_port
        self._ctx = context

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_host, self._connect_port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


class _ConnectToHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, connect_host: str, connect_port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._connect_host = connect_host
        self._connect_port = connect_port

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_host, self._connect_port), self.timeout)


@dataclass
class Fetched:
    status: int
    headers: Dict[str, str]


def _split_hostport(value: str, default_port: int) -> Tuple[str, int]:
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if value.count(":") == 1:
        host, port = value.split(":")
        return host, int(port)
    return value, default_port


def fetch(url: str, *, connect_to: Optional[str], timeout: float, user_agent: str) -> Fetched:
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ToolError(f"bad smoke URL {url!r}")
    port = u.port or (443 if u.scheme == "https" else 80)
    target = u.path or "/"
    if u.query:
        target += "?" + u.query
    ch, cp = _split_hostport(connect_to, port) if connect_to else (u.hostname, port)
    conn: http.client.HTTPConnection
    if u.scheme == "https":
        conn = _ConnectToHTTPS(u.hostname, port, ch, cp, timeout, ssl.create_default_context())
    else:
        conn = _ConnectToHTTP(u.hostname, port, ch, cp, timeout)
    try:
        conn.request("GET", target, headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        resp = conn.getresponse()
        resp.read(512 * 1024)
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return Fetched(resp.status, headers)
    finally:
        conn.close()


def evaluate(spec: PathSpec, got: Fetched, max_shared_age: int) -> List[str]:
    problems: List[str] = []
    if got.status != spec.status:
        problems.append(f"HTTP {got.status}, expected {spec.status}")
    if is_html(got):
        targeted = {h: got.headers[h] for h in TARGETED_CACHE_HEADERS if h in got.headers}
        problems += html_cache_problems(got.headers.get("cache-control"), got.headers.get("age"), max_shared_age,
                                        targeted)
    return problems


def is_html(got: Fetched) -> bool:
    return got.headers.get("content-type", "").lower().startswith("text/html")


def cmd_smoke(args: argparse.Namespace) -> int:
    specs = [parse_path_spec(p) for p in args.path]
    if not specs:
        raise ToolError("no smoke paths")
    base = args.url.rstrip("/")
    host = urllib.parse.urlsplit(base).hostname or ""
    label = args.label or base
    if args.optional and not args.connect_to:
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            print(f"skip  {label}: {host} does not resolve yet (DNS not set up)")
            return EXIT_OK
    via = f" via {args.connect_to}" if args.connect_to else ""
    print(f"smoke {label}: {base}{via} (HTML s-maxage <= {args.max_shared_age}s)")
    deadline = time.monotonic() + args.timeout
    failures = 0
    html_paths: List[str] = []
    for spec in specs:
        attempt = 0
        last: List[str] = []
        detail = ""
        while True:
            attempt += 1
            try:
                got = fetch(base + spec.path, connect_to=args.connect_to,
                            timeout=min(args.request_timeout, max(1.0, deadline - time.monotonic())),
                            user_agent=args.user_agent)
                if is_html(got) and spec.path not in html_paths:
                    html_paths.append(spec.path)
                last = evaluate(spec, got, args.max_shared_age)
                ctype = got.headers.get("content-type", "").split(";")[0] or "-"
                cc = got.headers.get("cache-control", "-")
                detail = f"{got.status} {ctype} cache-control: {cc}"
            except (OSError, http.client.HTTPException) as exc:
                last = [f"{type(exc).__name__}: {exc}"]
                detail = "no response"
            if not last or time.monotonic() >= deadline:
                break
            time.sleep(min(2.0 * attempt, 10.0, max(0.0, deadline - time.monotonic())))
        if last:
            failures += 1
            print(f"FAIL  {spec.path} ({detail}; {attempt} attempt(s)): " + "; ".join(last))
        else:
            print(f"ok    {spec.path} ({detail})")
    if len(html_paths) < 2:
        # The cache rule matters most on the pages that quote prices; / alone
        # does not show that a product page or a best-of list keeps it.
        print(f"WARN  {label}: the HTML cache rule was checked on {len(html_paths)} HTML page(s) "
              f"({', '.join(html_paths) or 'none'}); add a product page and a best-of list to SMOKE_PATHS")
    if failures:
        print(f"smoke {label}: {failures} of {len(specs)} path(s) failed")
        return EXIT_FAILED
    print(f"smoke {label}: all {len(specs)} path(s) passed")
    return EXIT_OK


# ─── JSON documents ───────────────────────────────────────────────────────────

def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _pairs(items: Sequence[str], what: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ToolError(f"{what} {item!r} must be NAME=VALUE")
        if name in seen:
            raise ToolError(f"{what} {name!r} given twice")
        seen.add(name)
        out.append((name, value))
    return out


def cmd_json_from_files(args: argparse.Namespace) -> int:
    doc: Dict[str, str] = {}
    for name, path in _pairs(args.pairs, "secret"):
        value = read_secret_file(path)
        if not value:
            raise ToolError(f"secret {name!r} is empty")
        doc[name] = value
    write_private(args.out, json.dumps(doc, sort_keys=True))
    return EXIT_OK


#: Fargate CPU units -> allowed memory (MiB).
FARGATE_SIZES: Dict[int, List[int]] = {
    256: [512, 1024, 2048],
    512: list(range(1024, 4096 + 1, 1024)),
    1024: list(range(2048, 8192 + 1, 1024)),
    2048: list(range(4096, 16384 + 1, 1024)),
    4096: list(range(8192, 30720 + 1, 1024)),
}


def cmd_ecs_taskdef(args: argparse.Namespace) -> int:
    if args.memory_mib not in FARGATE_SIZES.get(args.cpu_units, []):
        raise ToolError(f"Fargate has no {args.cpu_units} CPU units / {args.memory_mib} MiB size; "
                        f"valid memory for {args.cpu_units}: {FARGATE_SIZES.get(args.cpu_units, 'none')}")
    env = [{"name": k, "value": v} for k, v in _pairs(args.env or [], "env")]
    secret_refs = [{"name": k, "valueFrom": v} for k, v in _pairs(args.secret or [], "secret")]
    clash = {e["name"] for e in env} & {s["name"] for s in secret_refs}
    if clash:
        raise ToolError("names set both as plain env and as secrets: " + ", ".join(sorted(clash)))
    for ref in secret_refs:
        if not ref["valueFrom"].startswith("arn:"):
            raise ToolError(f"secret {ref['name']!r} must reference an ARN")
    doc = {
        "family": args.family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": str(args.cpu_units),
        "memory": str(args.memory_mib),
        "runtimePlatform": {"cpuArchitecture": args.arch, "operatingSystemFamily": "LINUX"},
        "executionRoleArn": args.exec_role_arn,
        "taskRoleArn": args.task_role_arn,
        "containerDefinitions": [{
            "name": args.container_name,
            "image": args.image,
            "essential": True,
            "portMappings": [{"containerPort": args.port, "protocol": "tcp"}],
            "environment": env,
            "secrets": secret_refs,
            "healthCheck": {
                "command": ["CMD-SHELL", f"curl -fsS http://127.0.0.1:{args.port}{args.health_path} >/dev/null || exit 1"],
                "interval": 30, "timeout": 10, "retries": 3, "startPeriod": 40,
            },
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {"awslogs-group": args.log_group, "awslogs-region": args.region,
                            "awslogs-stream-prefix": args.stream_prefix},
            },
        }],
        "tags": [{"key": "affiliate-kit-app", "value": args.family}],
    }
    text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
    if args.out in (None, "-"):
        sys.stdout.write(text)
    else:
        write_private(args.out, text)
    return EXIT_OK


def cmd_scaling_policy(args: argparse.Namespace) -> int:
    if not (10 <= args.target_cpu <= 90):
        raise ToolError("target CPU utilisation must be 10..90 percent")
    doc = {
        "TargetValue": float(args.target_cpu),
        "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"},
        "ScaleOutCooldown": 60,
        "ScaleInCooldown": 300,
    }
    text = json.dumps(doc, indent=2) + "\n"
    if args.out in (None, "-"):
        sys.stdout.write(text)
    else:
        write_private(args.out, text)
    return EXIT_OK


# ─── DNS (DNS-over-HTTPS) ─────────────────────────────────────────────────────

DNS_TYPES = {"A": 1, "CNAME": 5, "TXT": 16, "CAA": 257}
DEFAULT_DOH = ["https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"]


def doh_query(name: str, rtype: str, endpoints: Sequence[str], timeout: float) -> List[str]:
    """Answer data of type `rtype` for `name` (CNAME targets without the trailing dot,
    TXT strings unquoted and joined). Raises ToolError(EXIT_UNREACHABLE) when no resolver answers."""
    last = ""
    for ep in endpoints:
        q = urllib.parse.urlencode({"name": name, "type": rtype})
        req = urllib.request.Request(f"{ep}?{q}", headers={"accept": "application/dns-json",
                                                           "user-agent": "affiliate-kit-deploy/dns-check"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            last = f"{ep}: {exc}"
            continue
        want = DNS_TYPES[rtype]
        out: List[str] = []
        for ans in data.get("Answer") or []:
            if ans.get("type") != want:
                continue
            value = str(ans.get("data", ""))
            if rtype == "TXT":
                value = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', value)) or value
            if rtype == "CNAME":
                value = value.rstrip(".").lower()
            out.append(value)
        return out
    raise ToolError(f"no DNS-over-HTTPS resolver answered ({last})", EXIT_UNREACHABLE)


def cmd_dns_check(args: argparse.Namespace) -> int:
    endpoints = [args.doh] if args.doh else ([os.environ["KIT_DEPLOY_DOH_URL"]]
                                             if os.environ.get("KIT_DEPLOY_DOH_URL") else DEFAULT_DOH)
    host = args.host.rstrip(".").lower()
    bad = 0
    if args.expect_a:
        got = doh_query(host, "A", endpoints, args.timeout)
        if sorted(set(got)) == [args.expect_a]:
            print(f"ok    A      {host} -> {args.expect_a}")
        else:
            bad += 1
            seen = ", ".join(sorted(set(got))) or "nothing"
            print(f"WRONG A      {host} -> {seen}; expected only {args.expect_a} "
                  f"(a proxied/CDN record hides the origin; managed certificates need DNS-only)")
    if args.expect_cname:
        want = args.expect_cname.rstrip(".").lower()
        got = doh_query(host, "CNAME", endpoints, args.timeout)
        if got == [want]:
            print(f"ok    CNAME  {host} -> {want}")
        else:
            bad += 1
            seen = ", ".join(got) or "no CNAME record"
            print(f"WRONG CNAME  {host} -> {seen}; expected {want} "
                  f"(a proxied/CDN record answers with A records instead; managed certificates need a DNS-only CNAME)")
    for name, value in _pairs(args.expect_txt or [], "TXT"):
        name = name.rstrip(".").lower()
        got = doh_query(name, "TXT", endpoints, args.timeout)
        if value in got:
            print(f"ok    TXT    {name} = {value}")
        else:
            bad += 1
            seen = ", ".join(got) or "no TXT record"
            print(f"WRONG TXT    {name} = {seen}; expected {value}")
    return EXIT_OK if not bad else EXIT_MISMATCH


# ─── image users (teardown --with-images) ─────────────────────────────────────

def _strings(value: object) -> Iterator[str]:
    """Every string in a nested list (None and non-strings skipped)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def image_users(items: object, registry: str, repository: str, exclude_app: str = "") -> List[str]:
    """Resource ids in `items` ([{id, images}], az's --query output) that run an
    image from REGISTRY/REPOSITORY: any tag or digest, never a repository that
    merely starts with the same name. `exclude_app` (RESOURCE_GROUP/NAME) is the
    site's own Container App, matched by resource id, case-insensitively."""
    repo = f"{registry.strip().lower()}/{repository.strip().lower()}"
    skip = ""
    if exclude_app:
        group, _, name = exclude_app.partition("/")
        if not group or not name:
            raise ToolError(f"--exclude-app must be RESOURCE_GROUP/NAME (got {exclude_app!r})")
        skip = f"/resourcegroups/{group.lower()}/providers/microsoft.app/containerapps/{name.lower()}"
    if not isinstance(items, list):
        raise ToolError("expected a JSON list of {id, images} objects")
    users = set()
    for item in items:
        if not isinstance(item, dict):
            raise ToolError("expected a JSON list of {id, images} objects")
        rid = str(item.get("id") or "")
        if skip and rid.lower().endswith(skip):
            continue
        for image in _strings(item.get("images")):
            ref = image.strip().lower()
            if ref == repo or ref.startswith(repo + ":") or ref.startswith(repo + "@"):
                users.add(rid or "(a resource without an id)")
    return sorted(users)


def cmd_image_users(args: argparse.Namespace) -> int:
    """stdin: one or more JSON lists (az containerapp list, then job list); print each user's id."""
    text = sys.stdin.read()
    decoder = json.JSONDecoder()
    at, users = 0, set()
    while True:
        while at < len(text) and text[at].isspace():
            at += 1
        if at >= len(text):
            break
        try:
            doc, at = decoder.raw_decode(text, at)
        except ValueError as exc:
            raise ToolError(f"stdin is not JSON: {exc}") from None
        users.update(image_users(doc, args.registry, args.repository, args.exclude_app or ""))
    for rid in sorted(users):
        print(rid)
    return EXIT_OK


# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deploytool", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("env-dump", help="parse an env file; print KEY\\0VALUE\\0 pairs")
    s.add_argument("file")
    s.add_argument("--strict", action="store_true", help="every line must be a valid assignment")
    s.add_argument("--keys", help="comma-separated keys to output (default: all)")
    s.add_argument("--check", action="store_true", help="validate only; print nothing")
    s.set_defaults(fn=cmd_env_dump)

    s = sub.add_parser("dsn-info", help="describe a DSN read from a file (never the password); exit 3 on a mismatch")
    s.add_argument("--dsn-file", required=True)
    s.add_argument("--expect-user")
    s.add_argument("--expect-db")
    s.add_argument("--label")
    s.set_defaults(fn=cmd_dsn_info)

    s = sub.add_parser("role-verifier", help="SCRAM-SHA-256 verifier for the password in a DSN file")
    s.add_argument("--dsn-file", required=True)
    s.add_argument("--expect-user")
    s.add_argument("--expect-db")
    s.add_argument("--expect-host")
    s.add_argument("--expect-port", type=int)
    s.add_argument("--label")
    s.add_argument("--iterations", type=int, default=4096)
    s.add_argument("--salt-b64", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_role_verifier)

    s = sub.add_parser("new-dsn", help="generate a role password; append VAR='dsn' to a 0600 file; print its verifier")
    for flag in ("--user", "--host", "--db", "--var", "--out"):
        s.add_argument(flag, required=True)
    s.add_argument("--port", type=int, default=5432)
    s.add_argument("--sslmode", default="require")
    s.set_defaults(fn=cmd_new_dsn)

    s = sub.add_parser("check-cache-header", help="apply the HTML Cache-Control rule to one header value")
    s.add_argument("--cache-control", default="")
    s.add_argument("--missing", action="store_true", help="the response had no Cache-Control header")
    s.add_argument("--age")
    s.add_argument("--targeted", action="append", metavar="HEADER=VALUE",
                   help="a CDN-targeted header: " + ", ".join(TARGETED_CACHE_HEADERS) + " (repeatable)")
    s.add_argument("--max", type=int, default=600)
    s.set_defaults(fn=cmd_check_cache_header)

    s = sub.add_parser("site-cache", help="check site.json cache lifetimes against the limit")
    s.add_argument("--site-json", required=True)
    s.add_argument("--max", type=int, default=600)
    s.set_defaults(fn=cmd_site_cache)

    s = sub.add_parser("render-dockerfile", help="render Dockerfile.azure from the kit template")
    s.add_argument("--template", required=True)
    s.add_argument("--app-name", required=True)
    s.add_argument("--kit-version", required=True)
    s.add_argument("--node-image", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--server-entry", required=True)
    s.add_argument("--build-script", required=True)
    s.add_argument("--health-path", required=True)
    s.add_argument("--extra-paths", default="")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--out", help="write here ('-' = stdout, the default)")
    g.add_argument("--check", metavar="FILE", help="exit 3 if FILE differs from the rendering")
    s.set_defaults(fn=cmd_render_dockerfile)

    s = sub.add_parser("smoke", help="post-deploy HTTP checks")
    s.add_argument("--url", required=True, help="base URL, e.g. https://example.com")
    s.add_argument("--connect-to", help="HOST[:PORT] to connect to instead of resolving the URL host")
    s.add_argument("--optional", action="store_true", help="skip (exit 0) when the URL host does not resolve")
    s.add_argument("--path", action="append", default=[], help="/path or /path=<status> (repeatable)")
    s.add_argument("--max-shared-age", type=int, default=600)
    s.add_argument("--timeout", type=float, default=180.0, help="overall deadline in seconds")
    s.add_argument("--request-timeout", type=float, default=15.0)
    s.add_argument("--user-agent", default="affiliate-kit-deploy-smoke")
    s.add_argument("--label")
    s.set_defaults(fn=cmd_smoke)

    s = sub.add_parser("json-from-files", help="write {NAME: <file content>} to a 0600 file")
    s.add_argument("--out", required=True)
    s.add_argument("pairs", nargs="+", metavar="NAME=FILE")
    s.set_defaults(fn=cmd_json_from_files)

    s = sub.add_parser("ecs-taskdef", help="ECS Fargate task definition JSON")
    for flag in ("--family", "--container-name", "--image", "--exec-role-arn", "--task-role-arn",
                 "--log-group", "--region", "--stream-prefix"):
        s.add_argument(flag, required=True)
    s.add_argument("--cpu-units", type=int, required=True)
    s.add_argument("--memory-mib", type=int, required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--health-path", default="/health")
    s.add_argument("--arch", default="X86_64", choices=["X86_64", "ARM64"])
    s.add_argument("--env", action="append", metavar="NAME=VALUE")
    s.add_argument("--secret", action="append", metavar="NAME=ARN")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_ecs_taskdef)

    s = sub.add_parser("scaling-policy", help="target-tracking scaling policy JSON")
    s.add_argument("--target-cpu", type=int, default=60)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_scaling_policy)

    s = sub.add_parser("dns-check", help="check DNS records over DNS-over-HTTPS")
    s.add_argument("--host", required=True)
    s.add_argument("--expect-a")
    s.add_argument("--expect-cname")
    s.add_argument("--expect-txt", action="append", metavar="NAME=VALUE")
    s.add_argument("--doh", help="DNS-over-HTTPS JSON endpoint (default: Cloudflare, then Google)")
    s.add_argument("--timeout", type=float, default=10.0)
    s.set_defaults(fn=cmd_dns_check)

    s = sub.add_parser("image-users", help="resource ids (from az list JSON on stdin) running images from a repository")
    s.add_argument("--registry", required=True, help="login server, e.g. myacr.azurecr.io")
    s.add_argument("--repository", required=True)
    s.add_argument("--exclude-app", help="RESOURCE_GROUP/NAME of the site's own Container App")
    s.set_defaults(fn=cmd_image_users)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except ToolError as exc:
        print(f"deploytool {args.cmd}: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
