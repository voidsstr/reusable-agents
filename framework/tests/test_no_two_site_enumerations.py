"""F0 — no NEW code may enumerate the reference sites.

Why this exists
---------------
Per-site behaviour must come from config (storage configs, ``site.yaml``,
manifest fields and, once F1a/W1 land, ``framework.core.site_registry``),
never from code that lists the sites. Code that does list them silently
mishandles a third site: the backlog dispatcher never dispatches its
producers, ``verifications.py`` checks its recs against another site's
domain, the starvation boost favours it forever, and so on.

This test scans ``framework/`` and ``agents/`` for code constructs that name
two or more of the reference sites (listed in the allowlist file, so this
module itself names no site) and asserts that every one it finds is recorded
in ``framework/tests/data/two_site_enumerations_allowlist.json``. That file is
W1's work inventory: W1 replaces each entry with a site-registry lookup and
deletes the entry, until the list is empty. A construct that is not in the
allowlist fails the test, so no new enumeration can land.

What counts
-----------
A *site mention* is a site id inside a value-like string literal (one with no
inner whitespace: ``"<site>"``, ``"<site>.com"``, ``"<site>-seo-agent"``,
``"DATABASE_URL_<SITE>"``, a repo path) or inside an identifier
(``<SITE>_DSN``). Prose strings (log lines, help text, SQL, HTML) are ignored,
and so are comments and docstrings ("comment-only hits are allowed").

A *hit* is the innermost construct whose mentions span two or more sites:

* Python: tuple / list / set / dict displays, calls, boolean chains,
  ``if``/``elif`` chains, conditional expressions and ``match`` statements;
  as a catch-all, a function, class or module body. A def's header
  (decorators, bases, parameter defaults, annotations) belongs to the def, so
  ``def f(root="<a>.com"): return root + "?tag=<b>-20"`` is one hit.
* Shell: one logical line (continuations joined), ``if``/``case`` blocks,
  multi-line arrays, a function or script body. Python programs embedded in
  a script (heredocs fed to ``python``, ``<<-`` included, and
  ``python3 -c '<program>'``) are scanned with the Python rules, after the
  shell's own escape and expansion handling. One that still does not parse
  is a scan error, so the gate never silently stops looking inside it.
* JS/TS: the innermost ``{}``, ``[]`` or ``()`` region, or a top-level
  declaration.

More shapes count even though they spell out a single literal or site
(kind ``string`` unless noted):

* a string that is itself an alternation of sites (``"<a>|<b>"``: a regex, or
  a usage message such as ``set X=<a|b>``);
* a string that is a list of sites: every piece between whitespace, ``,``,
  ``;`` or ``|`` names one (``"<a> <b>"``, ``"<a>, <b>"``); prose such as
  ``"e.g. <a>, <b>"`` has pieces that name none and is not a list;
* a string of code that quotes two site ids (SQL ``IN ('<a>','<b>')``, JSON
  ``{"<a>": ...}``). This check is case-sensitive: a quoted display name in
  help text (``'<A>Site'``) is a label, not a value;
* in a heredoc that is not Python (SQL, a prompt), a line that does any of
  the above;
* a branch whose condition names exactly one site and that has a fallback
  branch (``x if site == "<a>" else y``; ``if site == "<a>": ... else: ...``;
  a ``case`` with ``*)``). With two sites the fallback *is* the other site,
  so a third site would silently be treated as it. Kind
  ``<construct>:implicit-other``.

A construct's mentions are consumed by the innermost hit, so nesting does not
double-report, and an outer construct is reported only if it enumerates the
sites by itself.

Allowlist identity
------------------
Entries match on ``(path, scope, kind, size, tokens)`` plus a ``count``, never
on line numbers, which drift. ``scope`` is the enclosing function/class
qualname, ``size`` the construct's arity (elements, keys or branches) and
``tokens`` the distinct site-naming literals and identifiers. Moving code
around is fine; growing an allowlisted enumeration (say, adding a third
site's id to it) changes its key and fails, which is the point. ``line`` and
``snippet`` in each entry are for humans only.

Maintenance
-----------
* A new hit you did not mean to add: lift the per-site values into config.
* Fixed an entry (W1): delete it; ``test_allowlist_has_no_stale_entries``
  names it.
* A deliberate, reviewed exception: add the entry printed by
  ``python3 framework/tests/test_no_two_site_enumerations.py --new`` and
  fill in its ``w1`` note (the well-formedness test rejects an empty one).
* ``sanctioned_paths`` lists files that may enumerate the sites by design
  (the site registry's built-in defaults).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = Path(__file__).resolve().parent / "data" / "two_site_enumerations_allowlist.json"

#: Top-level directories that hold framework and shipped-agent code.
SCAN_ROOTS: tuple[str, ...] = ("framework", "agents")
PY_SUFFIXES = frozenset({".py"})
SHELL_SUFFIXES = frozenset({".sh", ".bash"})
JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})
#: Directory names never scanned: caches, vendored or built output, test code
#: and fixtures (tests name both sites on purpose) and retired agents.
SKIP_DIR_NAMES = frozenset({
    "__pycache__", "node_modules", ".venv", "venv", ".git", ".pytest_cache",
    ".mypy_cache", "dist", "build", "tests", "test", "__tests__", "fixtures",
    "_archive",
})
_TEST_FILE_RE = re.compile(
    r"^(test_.+\.py|.+_test\.py|conftest\.py|.+\.(spec|test)\.(js|mjs|cjs|jsx|ts|tsx))$")

ALLOWLIST_SCHEMA_VERSION = 1
_ENTRY_FIELDS = ("path", "scope", "kind", "size", "tokens", "count", "line", "snippet", "w1")
_MAX_TOKEN_CHARS = 160
_MAX_SNIPPET_CHARS = 120
_IMPLICIT = ":implicit-other"
#: Separators of a value list spelled as one string ("a b", "a, b", "a;b").
_LIST_SEPARATORS_RE = re.compile(r"[\s,;|]+")
#: A whitespace-free value quoted inside a string, as SQL, JSON or code quote one.
_QUOTED_VALUE_RE = re.compile(r"""(['"`])([^'"`\s]+)\1""")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Mention:
    """One site id spotted in a value-like literal or an identifier."""

    site: str
    token: str
    line: int


@dataclass(frozen=True)
class Hit:
    """A construct that enumerates the reference sites."""

    path: str
    scope: str
    kind: str
    size: int
    tokens: tuple[str, ...]
    sites: tuple[str, ...]
    line: int
    end_line: int
    snippet: str

    @property
    def key(self) -> tuple[str, str, str, int, tuple[str, ...]]:
        return (self.path, self.scope, self.kind, self.size, self.tokens)

    def location(self) -> str:
        span = str(self.line) if self.end_line == self.line else f"{self.line}-{self.end_line}"
        return f"{self.path}:{span}"

    def as_entry(self, *, count: int = 1, w1: str = "") -> dict:
        return {
            "path": self.path, "scope": self.scope, "kind": self.kind,
            "size": self.size, "tokens": list(self.tokens), "count": count,
            "line": self.line, "snippet": self.snippet, "w1": w1,
        }


def _sites_of(mentions: Iterable[Mention]) -> set[str]:
    return {m.site for m in mentions}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" in text else text
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _make_hit(*, path: str, scope: str, kind: str, size: int, mentions: Iterable[Mention],
              line: int, end_line: int, snippet: str) -> Hit:
    mentions = list(mentions)
    return Hit(path=path, scope=scope, kind=kind, size=size,
               tokens=tuple(sorted({m.token for m in mentions})),
               sites=tuple(sorted(_sites_of(mentions))),
               line=line, end_line=max(line, end_line), snippet=snippet)


def _snippet(lines: Sequence[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return _clip(lines[lineno - 1].strip(), _MAX_SNIPPET_CHARS)
    return ""


class SiteMatcher:
    """Finds reference-site ids in literals and identifiers (case-insensitive)."""

    def __init__(self, sites: Sequence[str]):
        cleaned = tuple(dict.fromkeys(s.strip().lower() for s in sites if s and s.strip()))
        if len(cleaned) < 2:
            raise ValueError("need at least two reference sites to detect enumerations")
        self.sites = cleaned
        alternatives = sorted(cleaned, key=len, reverse=True)
        self._re = re.compile("|".join(re.escape(s) for s in alternatives), re.IGNORECASE)
        self._pipe_ws = re.compile(r"\s*\|\s*")

    def sites_in(self, text: str) -> set[str]:
        return {m.group(0).lower() for m in self._re.finditer(text)} if text else set()

    def mentions_in_identifier(self, name: str, line: int) -> set[Mention]:
        return {Mention(s, _clip(name, _MAX_TOKEN_CHARS), line) for s in self.sites_in(name)}

    def is_alternation(self, text: str) -> bool:
        """``a|b`` naming two sites in one whitespace-free run: a regex
        alternation or a usage message such as ``SITE=<a|b>``."""
        if "|" not in text:
            return False
        norm = self._pipe_ws.sub("|", text)
        return any("|" in tok and len(self.sites_in(tok)) >= 2 for tok in norm.split())

    def is_site_list(self, text: str) -> bool:
        """``"<a> <b>"``, ``"<a>, <b>"``, ``"<a>-x;<b>-x"``: every piece between
        separators (whitespace, ``,``, ``;``, ``|``) names a site. Prose such as
        ``"e.g. <a>, <b>"`` or ``"<a> then <b>"`` has a piece that names none."""
        pieces = [p for p in _LIST_SEPARATORS_RE.split(text) if p]
        return (len(pieces) >= 2 and all(self.sites_in(p) for p in pieces)
                and len(self.sites_in(text)) >= 2)

    def quotes_sites(self, text: str) -> bool:
        """Code embedded in a string that quotes two site ids as values: SQL
        ``IN ('<a>', '<b>')``, JSON ``{"<a>": 1, "<b>": 2}``, a Python snippet.

        Case-sensitive on purpose: code spells an id in lower case (or upper
        case, as in ``'<A>_DSN'``); a quoted display name such as ``'AcmeSite'``
        is a label in prose (help text giving examples), not a value."""
        quoted: set[str] = set()
        for m in _QUOTED_VALUE_RE.finditer(text):
            value = m.group(2)
            quoted.update(s for s in self.sites if s in value or s.upper() in value)
        return len(quoted) >= 2

    def enumerates_inline(self, text: str) -> bool:
        """The text lists the sites by itself, even with inner whitespace."""
        return self.is_alternation(text) or self.is_site_list(text) or self.quotes_sites(text)

    def classify_literal(self, value: str, line: int) -> tuple[set[Mention], bool]:
        """Return ``(mentions, enumerates)`` for one string literal.

        ``enumerates`` is True when the literal alone names two sites as values
        (``"a,b"``, ``"a/b"``, ``"a b"``), is an alternation, or quotes two
        sites as code does (``IN ('a', 'b')``), even inside prose. Prose (inner
        whitespace) otherwise contributes no mentions.
        """
        named = self.sites_in(value)
        if not named:
            return set(), False
        token = _clip(value.strip(), _MAX_TOKEN_CHARS)
        mentions = {Mention(s, token, line) for s in named}
        if len(named) >= 2 and self.enumerates_inline(value):
            return mentions, True
        if any(ch.isspace() for ch in value.strip()):
            return set(), False
        return mentions, len(named) >= 2


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

_PY_CONTAINER_KINDS: dict[type, str] = {
    ast.Tuple: "tuple", ast.List: "list", ast.Set: "set", ast.Dict: "dict",
    ast.Call: "call", ast.BoolOp: "boolop",
}
_AST_MATCH = getattr(ast, "Match", None)


def _is_bare_string(node: ast.AST) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _py_size(node: ast.AST) -> int:
    if isinstance(node, ast.Dict):
        return len(node.keys)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return len(node.elts)
    if isinstance(node, ast.Call):
        return len(node.args) + len(node.keywords)
    if isinstance(node, ast.BoolOp):
        return len(node.values)
    return 0


def parse_python(source: str, filename: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # SyntaxWarning on odd escapes is not our business
        return ast.parse(source, filename=filename)


class PythonScanner:
    """AST walk that reports the innermost constructs enumerating the sites."""

    def __init__(self, *, path: str, source: str, matcher: SiteMatcher,
                 line_offset: int = 0, scope_prefix: str = ""):
        self.path = path
        self.lines = source.splitlines()
        self.matcher = matcher
        self.offset = line_offset
        self.prefix = scope_prefix
        self.scopes: list[str] = []
        self.hits: list[Hit] = []

    def scan(self, tree: ast.Module) -> list[Hit]:
        self._flush_block(self._stmts(tree.body))
        return self.hits

    # -- helpers ------------------------------------------------------------
    def _scope(self) -> str:
        inner = ".".join(self.scopes)
        if self.prefix:
            return f"{self.prefix}/{inner}" if inner else self.prefix
        return inner or "<module>"

    def _hit(self, kind: str, mentions: Iterable[Mention], *, size: int,
             line: int, end_line: int) -> None:
        """``line``/``end_line`` are relative to ``self.lines``."""
        self.hits.append(_make_hit(
            path=self.path, scope=self._scope(), kind=kind, size=size, mentions=mentions,
            line=line + self.offset, end_line=end_line + self.offset,
            snippet=_snippet(self.lines, line)))

    def _node_hit(self, node: ast.AST, kind: str, mentions: Iterable[Mention], *,
                  size: int, end_line: Optional[int] = None) -> None:
        line = getattr(node, "lineno", 1)
        self._hit(kind, mentions, size=size, line=line,
                  end_line=end_line or getattr(node, "end_lineno", None) or line)

    def _flush_block(self, mentions: set[Mention]) -> None:
        """Catch-all: a def/class/module body whose loose statements name the sites."""
        if len(_sites_of(mentions)) >= 2:
            lines = sorted(m.line - self.offset for m in mentions)
            self._hit("block", mentions, size=0, line=lines[0], end_line=lines[-1])

    def _stmts(self, stmts: Sequence[ast.stmt]) -> set[Mention]:
        found: set[Mention] = set()
        for stmt in stmts:
            found |= self._visit(stmt)
        return found

    # -- visitor ------------------------------------------------------------
    def _visit(self, node: ast.AST) -> set[Mention]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return self._visit_def(node)
        if isinstance(node, ast.If):
            return self._visit_if(node)
        if isinstance(node, ast.IfExp):
            return self._visit_ifexp(node)
        if _AST_MATCH is not None and isinstance(node, _AST_MATCH):
            return self._visit_match(node)
        if _is_bare_string(node):
            return set()  # docstring, or a string used as a comment
        if isinstance(node, ast.Constant):
            return self._visit_constant(node)

        found = self._identifier_mentions(node)
        for child in ast.iter_child_nodes(node):
            found |= self._visit(child)
        kind = _PY_CONTAINER_KINDS.get(type(node))
        if kind is not None and len(_sites_of(found)) >= 2:
            self._node_hit(node, kind, found, size=_py_size(node))
            return set()
        return found

    def _identifier_mentions(self, node: ast.AST) -> set[Mention]:
        line = getattr(node, "lineno", 0) + self.offset
        if isinstance(node, ast.Name):
            return self.matcher.mentions_in_identifier(node.id, line)
        if isinstance(node, ast.Attribute):
            return self.matcher.mentions_in_identifier(node.attr, line)
        if isinstance(node, ast.keyword) and node.arg:
            return self.matcher.mentions_in_identifier(node.arg, line)
        return set()

    def _visit_constant(self, node: ast.Constant) -> set[Mention]:
        if not isinstance(node.value, str):
            return set()
        mentions, enumerates = self.matcher.classify_literal(
            node.value, getattr(node, "lineno", 0) + self.offset)
        if enumerates:
            self._node_hit(node, "string", mentions, size=1)
            return set()
        return mentions

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[Mention]:
        # The header (decorators, bases/keywords, parameter defaults and
        # annotations) is evaluated in the enclosing scope, so a container hit
        # in it keeps the enclosing scope. Its loose mentions, though, belong to
        # the def: `def f(root="<a>.com"): ... "?tag=<b>-20"` is one function
        # that hard-codes both sites.
        header: set[Mention] = set()
        for dec in node.decorator_list:
            header |= self._visit(dec)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                header |= self._visit(base)
            for kw in node.keywords:
                header |= self._visit(kw)
        else:
            header |= self._visit(node.args)
            if node.returns is not None:
                header |= self._visit(node.returns)
        self.scopes.append(node.name)
        try:
            everything = header | self._stmts(node.body)
            if len(_sites_of(everything)) >= 2:
                self._flush_block(everything)
                return set()
        finally:
            self.scopes.pop()
        # A def is a boundary: its body never merges with siblings. A header
        # mention that formed no hit here still counts in the enclosing scope.
        return header

    def _branch(self, node: ast.AST, kind: str, test: set[Mention], rest: set[Mention], *,
                has_default: bool, size: int, end_line: int) -> set[Mention]:
        everything = test | rest
        if len(_sites_of(everything)) >= 2:
            self._node_hit(node, kind, everything, size=size, end_line=end_line)
            return set()
        if has_default and len(_sites_of(test)) == 1:
            self._node_hit(node, kind + _IMPLICIT, everything, size=size, end_line=end_line)
            return set()
        return everything

    def _visit_if(self, node: ast.If) -> set[Mention]:
        tests: list[ast.expr] = []
        bodies: list[list[ast.stmt]] = []
        cur = node
        while True:
            tests.append(cur.test)
            bodies.append(cur.body)
            if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                cur = cur.orelse[0]
                continue
            orelse = cur.orelse
            break
        test_m: set[Mention] = set()
        for t in tests:
            test_m |= self._visit(t)
        rest: set[Mention] = set()
        for body in bodies:
            rest |= self._stmts(body)
        rest |= self._stmts(orelse)
        last = orelse[-1] if orelse else bodies[-1][-1]
        return self._branch(node, "if-chain", test_m, rest, has_default=bool(orelse),
                            size=len(tests) + (1 if orelse else 0),
                            end_line=getattr(last, "end_lineno", node.lineno))

    def _visit_ifexp(self, node: ast.IfExp) -> set[Mention]:
        tests: list[ast.expr] = []
        branches: list[ast.expr] = []
        cur: ast.expr = node
        while isinstance(cur, ast.IfExp):
            tests.append(cur.test)
            branches.append(cur.body)
            cur = cur.orelse
        branches.append(cur)
        test_m: set[Mention] = set()
        for t in tests:
            test_m |= self._visit(t)
        rest: set[Mention] = set()
        for b in branches:
            rest |= self._visit(b)
        return self._branch(node, "ifexp", test_m, rest, has_default=True,
                            size=len(tests) + 1,
                            end_line=node.end_lineno or node.lineno)

    def _visit_match(self, node: ast.Match) -> set[Mention]:
        test_m = self._visit(node.subject)
        rest: set[Mention] = set()
        has_default = False
        for case in node.cases:
            test_m |= self._visit(case.pattern)
            if case.guard is not None:
                test_m |= self._visit(case.guard)
            if isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None:
                has_default = True  # `case _:` or a bare capture
            rest |= self._stmts(case.body)
        return self._branch(node, "match", test_m, rest, has_default=has_default,
                            size=len(node.cases),
                            end_line=node.end_lineno or node.lineno)


def scan_python_source(path: str, source: str, matcher: SiteMatcher, *,
                       line_offset: int = 0, scope_prefix: str = "") -> list[Hit]:
    tree = parse_python(source, path)
    return PythonScanner(path=path, source=source, matcher=matcher,
                         line_offset=line_offset, scope_prefix=scope_prefix).scan(tree)


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")
_PYTHON_CMD_RE = re.compile(r"(?:^|[\s;&|(`=])(?:\S*/)?python[0-9.]*(?=\s|$)")
#: A skeleton that ends in ``python3 [-flags] -c``: the next quoted region is a program.
_PYTHON_C_RE = re.compile(r"(?:^|[\s;&|(`=])(?:\S*/)?python[0-9.]*(?:\s+-[A-Za-z]+)*?\s+-[A-Za-z]*c\s*$")
_SH_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SH_SPECIAL_PARAMS = frozenset("@*#?$!-0123456789")
#: What a backslash escapes inside double quotes, and inside an unquoted heredoc.
_DQ_ESCAPABLE = '$`"\\\n'
_HEREDOC_ESCAPABLE = "$`\\\n"
_SH_STANDIN = "_sh_"


def _matching_close(text: str, start: int, opener: str, closer: str) -> int:
    """Offset of the ``closer`` balancing ``text[start] == opener`` (the end if none)."""
    depth = 0
    for j in range(start, len(text)):
        if text[j] == opener:
            depth += 1
        elif text[j] == closer:
            depth -= 1
            if depth == 0:
                return j
    return len(text) - 1


def shell_expanded(text: str, *, escapable: str, expand: bool) -> str:
    """``text`` as the program the shell hands to Python.

    Resolves backslash escapes (``escapable`` names the characters a backslash
    escapes in this context; ``\\<newline>`` joins lines). With ``expand``,
    stands in an identifier for each expansion so the result parses:
    ``$NAME`` / ``${NAME...}`` become ``NAME`` (a site named in the variable
    stays visible) and ``$(...)``, backticks and ``$1``-style parameters become
    ``_sh_``.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\\" and nxt and nxt in escapable:
            if nxt != "\n":
                out.append(nxt)
            i += 2
            continue
        if expand and ch == "$" and nxt == "{":
            end = _matching_close(text, i + 1, "{", "}")
            name = _SH_NAME_RE.match(text, i + 2, end)
            out.append(name.group(0) if name else _SH_STANDIN)
            i = end + 1
            continue
        if expand and ch == "$" and nxt == "(":
            i = _matching_close(text, i + 1, "(", ")") + 1
            out.append(_SH_STANDIN)
            continue
        if expand and ch == "$" and (nxt.isalpha() or nxt == "_"):
            name = _SH_NAME_RE.match(text, i + 1)
            assert name is not None
            out.append(name.group(0))
            i = name.end()
            continue
        if expand and ch == "$" and nxt and nxt in _SH_SPECIAL_PARAMS:
            out.append(_SH_STANDIN)
            i += 2
            continue
        if expand and ch == "`":
            close = text.find("`", i + 1)
            i = n if close < 0 else close + 1
            out.append(_SH_STANDIN)
            continue
        out.append(ch)
        i += 1
    return "".join(out)
_SH_TOKEN_RE = re.compile(r";;|&&|\|\||[;&|()]|[^\s;&|()]+")
_SH_WORD_RE = re.compile(r"[A-Za-z0-9_.\-/:@+]+")
_SH_PARAM_OP_RE = re.compile(r":[-=+?]")
_SH_FUNC_RE = re.compile(
    r"^\s*(?:function\s+([A-Za-z_][\w.:-]*)\s*(?:\(\s*\))?|([A-Za-z_][\w.:-]*)\s*\(\s*\))\s*(\{)?\s*$")
_SH_ARRAY_OPEN_RE = re.compile(r"^\s*(?:(?:local|declare|typeset|readonly)\s+(?:-\w+\s+)*)?"
                               r"[A-Za-z_]\w*\+?=\(\s*$")
#: After these tokens the next word is in command position.
_SH_CMD_RESET = frozenset({";", ";;", "&&", "||", "|", "&", "(", "then", "do", "else", "{", "!", "time"})
#: Closing keyword -> the block kinds it closes.
_SH_CLOSES = {"fi": ("if",), "esac": ("case",), "}": ("function", "group")}


@dataclass
class _Heredoc:
    tag: str
    python: bool
    start_line: int  # line number of the first body line
    body: str  # leading tabs already stripped for `<<-`
    expands: bool = False  # unquoted delimiter: the shell expands $... in the body


@dataclass
class _ShLine:
    """One logical shell line: comments removed, continuations joined."""

    start: int
    end: int
    code: str
    quoted_spans: list[tuple[int, int]]  # [start, end) of each top-level quoted region
    heredocs: list[_Heredoc]
    quoted_lines: list[int] = field(default_factory=list)  # line each quoted region opens on

    def skeleton(self) -> str:
        """``code`` with every quoted region collapsed to ``Q``."""
        return self.skeleton_and_marks()[0]

    def skeleton_and_marks(self) -> tuple[str, list[int]]:
        """``skeleton()`` plus the offset of the ``Q`` standing for each quoted region."""
        out, marks, pos, size = [], [], 0, 0
        for s, e in self.quoted_spans:
            chunk = self.code[pos:s]
            out.append(chunk)
            size += len(chunk)
            marks.append(size)
            out.append("Q")
            size += 1
            pos = e
        out.append(self.code[pos:])
        return "".join(out), marks

    def unquoted_text(self) -> str:
        out, pos = [], 0
        for s, e in self.quoted_spans:
            out.append(self.code[pos:s])
            out.append(" ")
            pos = e
        out.append(self.code[pos:])
        return "".join(out)

    def quoted_values(self) -> list[str]:
        values = []
        for s, e in self.quoted_spans:
            raw = self.code[s:e]
            if raw.startswith("$'"):
                raw = raw[2:-1] if raw.endswith("'") and len(raw) >= 3 else raw[2:]
            elif len(raw) >= 2 and raw[0] in "'\"`" and raw[-1] == raw[0]:
                raw = raw[1:-1]
            values.append(raw)
        return values


def split_shell(text: str) -> list[_ShLine]:
    """Tokenise a shell script into logical lines.

    Drops comments, joins backslash and ``|``/``&&``/``||`` continuations,
    keeps multi-line quoted strings inside their logical line, records each
    top-level quoted region, and lifts heredoc bodies out of the code
    (attaching them to the line whose operator opened them).
    """
    out: list[_ShLine] = []
    n = len(text)
    i = 0
    line = 1  # line number of text[i]
    start = 1
    code: list[str] = []  # one character per element, so len(code) is an offset
    spans: list[tuple[int, int]] = []
    span_lines: list[int] = []
    heredocs: list[_Heredoc] = []
    pending: list[tuple[str, bool, bool, bool]] = []  # (tag, strip_tabs, python, expands)
    # Context stack: "sq", "ansi", "dq", "bt", "cmdsub" (only inside quotes), "param".
    stack: list[str] = []
    depth: list[int] = []  # nesting counters for "cmdsub"/"param" entries
    quote_start = -1
    quote_line = 0

    def emit(s: str) -> None:
        code.extend(s)

    def at_word_start() -> bool:
        return not code or code[-1].isspace() or code[-1] in ";&|()"

    def open_quote(kind: str, opener: str) -> None:
        nonlocal quote_start, quote_line
        if not stack:
            quote_start, quote_line = len(code), line
        stack.append(kind)
        emit(opener)

    def close_quote() -> None:
        stack.pop()
        if not stack:
            spans.append((quote_start, len(code)))
            span_lines.append(quote_line)

    def finish_line(end_line: int) -> None:
        nonlocal code, spans, span_lines, heredocs
        joined = "".join(code)
        if joined.strip() or heredocs:
            out.append(_ShLine(start, end_line, joined, spans, heredocs, span_lines))
        code, spans, span_lines, heredocs = [], [], [], []

    def read_heredocs() -> None:
        """Consume pending heredoc bodies; ``text[i]`` starts the first body line."""
        nonlocal i, line
        for tag, strip_tabs, python, expands in pending:
            body_start = line
            body: list[str] = []
            while i < n:
                nl = text.find("\n", i)
                raw = text[i:] if nl < 0 else text[i:nl]
                i = n if nl < 0 else nl + 1
                line += 1
                if strip_tabs:  # `<<-` strips leading tabs from body lines and the delimiter
                    raw = raw.lstrip("\t")
                if raw == tag:
                    break
                body.append(raw)
            heredocs.append(_Heredoc(tag, python, body_start, "\n".join(body), expands))
        pending.clear()

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        top = stack[-1] if stack else None

        if top in ("sq", "ansi"):
            if top == "ansi" and ch == "\\" and nxt:
                emit(ch + nxt)
                line += nxt == "\n"
                i += 2
                continue
            emit(ch)
            line += ch == "\n"
            if ch == "'":
                close_quote()
            i += 1
            continue

        if top in ("dq", "bt"):
            if ch == "\\" and nxt:
                emit(ch + nxt)
                line += nxt == "\n"
                i += 2
                continue
            emit(ch)
            line += ch == "\n"
            if ch == ('"' if top == "dq" else "`"):
                close_quote()
            elif top == "dq" and ch == "$" and nxt and nxt in "({":
                emit(nxt)
                stack.append("cmdsub" if nxt == "(" else "param")
                depth.append(0)
                i += 2
                continue
            i += 1
            continue

        if top == "param":
            emit(ch)
            line += ch == "\n"
            if ch == "{":
                depth[-1] += 1
            elif ch == "}":
                if depth[-1] == 0:
                    stack.pop()
                    depth.pop()
                else:
                    depth[-1] -= 1
            elif ch == "'":
                stack.append("sq")
            elif ch == '"':
                stack.append("dq")
            i += 1
            continue

        # Code: top level, or a command substitution inside double quotes.
        if ch == "\\" and nxt == "\n":
            line += 1
            i += 2
            continue
        if ch == "\\" and nxt:
            emit(ch + nxt)
            i += 2
            continue
        if ch == "#" and at_word_start():
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if ch == "\n":
            line_end = line
            i += 1
            line += 1
            if pending:
                read_heredocs()
            if top == "cmdsub":
                emit(" ")
                continue
            stripped = "".join(code).rstrip()
            if stripped.endswith(("|", "&&", "||")):
                emit(" ")
                continue
            finish_line(line_end)
            start = line
            continue
        if top == "cmdsub":
            if ch == "(":
                depth[-1] += 1
            elif ch == ")":
                if depth[-1] == 0:
                    stack.pop()
                    depth.pop()
                    emit(ch)
                    i += 1
                    continue
                depth[-1] -= 1
        if ch == "<" and nxt == "<":
            if text[i + 2:i + 3] == "<":  # here-string, not a heredoc
                emit("<<<")
                i += 3
                continue
            m = _HEREDOC_RE.match(text, i)
            if m:
                tag = m.group(2) or m.group(3) or m.group(4)
                is_python = bool(_PYTHON_CMD_RE.search("".join(code)))
                # Any quoting of the delimiter (<<'X', <<"X", <<\X) turns expansion off.
                expands = m.group(4) is not None and "\\" not in m.group(0)
                pending.append((tag, m.group(1) == "-", is_python, expands))
                emit(m.group(0))
                i = m.end()
                continue
        if ch == "$" and nxt == "'" and not stack:
            open_quote("ansi", "$'")
            i += 2
            continue
        if ch == "$" and nxt == "{":
            emit("${")
            stack.append("param")
            depth.append(0)
            i += 2
            continue
        if ch in "'\"`":
            open_quote({"'": "sq", '"': "dq", "`": "bt"}[ch], ch)
            i += 1
            continue
        emit(ch)
        i += 1

    if pending:
        read_heredocs()
    finish_line(line)
    return out


@dataclass
class _ShBlock:
    kind: str  # "script" | "function" | "group" | "if" | "case" | "array"
    scope: str
    start: int
    test: set = field(default_factory=set)
    rest: set = field(default_factory=set)
    branches: int = 0
    has_default: bool = False
    in_patterns: bool = False


class ShellScanner:
    """Logical-line scan of a shell script (see the module docstring)."""

    def __init__(self, *, path: str, text: str, matcher: SiteMatcher):
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        self.matcher = matcher
        self.hits: list[Hit] = []
        self.errors: list[str] = []  # embedded python programs the scan cannot see inside

    def _hit(self, kind: str, mentions: Iterable[Mention], *, scope: str, size: int,
             line: int, end_line: int) -> None:
        self.hits.append(_make_hit(path=self.path, scope=scope, kind=kind, size=size,
                                   mentions=mentions, line=line, end_line=end_line,
                                   snippet=_snippet(self.lines, line)))

    def _value_mentions(self, value: str, ln: _ShLine, scope: str) -> set[Mention]:
        found: set[Mention] = set()
        # `${NAME:-default}`: the operator is not part of either word.
        for word in _SH_WORD_RE.findall(_SH_PARAM_OP_RE.sub(" ", value)):
            mentions, enumerates = self.matcher.classify_literal(word, ln.start)
            if enumerates:
                self._hit("string", mentions, scope=scope, size=1, line=ln.start, end_line=ln.end)
            else:
                found |= mentions
        return found

    def _line_mentions(self, ln: _ShLine, scope: str,
                       skip: frozenset[int] = frozenset()) -> set[Mention]:
        """``skip``: quoted regions already scanned as python programs."""
        found: set[Mention] = set()
        for k, value in enumerate(ln.quoted_values()):
            if k in skip:
                continue
            if self.matcher.is_alternation(value):
                mentions, _ = self.matcher.classify_literal(value, ln.start)
                self._hit("string", mentions, scope=scope, size=1, line=ln.start, end_line=ln.end)
            elif not any(ch.isspace() for ch in value.strip()):
                found |= self._value_mentions(value, ln, scope)
            elif self.matcher.enumerates_inline(value):  # "<a> <b>", IN ('<a>','<b>')
                mentions, _ = self.matcher.classify_literal(value, ln.start)
                self._hit("string", mentions, scope=scope, size=1, line=ln.start, end_line=ln.end)
        unquoted = ln.unquoted_text()
        if self.matcher.is_alternation(unquoted):
            mentions, _ = self.matcher.classify_literal(unquoted, ln.start)
            self._hit("string", mentions, scope=scope, size=1, line=ln.start, end_line=ln.end)
            return found
        return found | self._value_mentions(unquoted, ln, scope)

    def _close(self, blk: _ShBlock, stack: list[_ShBlock], end_line: int) -> None:
        everything = blk.test | blk.rest
        if blk.kind in ("if", "case", "array"):
            kind = {"if": "if-chain", "case": "case", "array": "array"}[blk.kind]
            size = 0 if blk.kind == "array" else blk.branches
            if len(_sites_of(everything)) >= 2:
                self._hit(kind, everything, scope=blk.scope, size=size,
                          line=blk.start, end_line=end_line)
                return
            if blk.kind != "array" and blk.has_default and len(_sites_of(blk.test)) == 1:
                self._hit(kind + _IMPLICIT, everything, scope=blk.scope, size=size,
                          line=blk.start, end_line=end_line)
                return
        elif blk.kind in ("function", "script"):
            if len(_sites_of(everything)) >= 2:
                lines = sorted(m.line for m in everything)
                self._hit("block", everything, scope=blk.scope, size=0,
                          line=lines[0], end_line=lines[-1])
            return  # function and script bodies are boundaries
        if stack:
            stack[-1].rest |= everything

    def scan(self) -> list[Hit]:
        stack: list[_ShBlock] = [_ShBlock("script", "<script>", 1)]
        pending_function: Optional[str] = None

        for ln in split_shell(self.text):
            scope = next((b.scope for b in reversed(stack) if b.kind == "function"), "<script>")
            programs = self._scan_python_c(ln, scope)
            mentions = self._line_mentions(ln, scope, skip=programs)
            if len(_sites_of(mentions)) >= 2:
                self._hit("line", mentions, scope=scope, size=0, line=ln.start, end_line=ln.end)
                mentions = set()
            skeleton = ln.skeleton()

            fn = _SH_FUNC_RE.match(skeleton)
            if fn:
                name = fn.group(1) or fn.group(2)
                if fn.group(3):
                    stack.append(_ShBlock("function", name, ln.start))
                else:
                    pending_function = name
                self._scan_heredocs(ln, name)
                continue

            if stack[-1].kind == "array":
                stack[-1].rest |= mentions
                if ")" in skeleton:
                    self._close(stack.pop(), stack, ln.end)
                self._scan_heredocs(ln, scope)
                continue
            if _SH_ARRAY_OPEN_RE.match(skeleton):
                stack.append(_ShBlock("array", scope, ln.start, rest=set(mentions)))
                self._scan_heredocs(ln, scope)
                continue

            tokens = _SH_TOKEN_RE.findall(skeleton)
            first = tokens[0] if tokens else ""
            innermost = stack[-1]
            if innermost.kind == "case" and innermost.in_patterns and first != "esac":
                head = skeleton.lstrip()
                close = head.find(")")
                if close > 0 and "(" not in head[:close].lstrip("("):
                    innermost.branches += 1
                    innermost.has_default |= head[:close].lstrip("(").strip() == "*"
                    innermost.test |= mentions
                    mentions = set()
                    tokens = _SH_TOKEN_RE.findall(head[close + 1:])

            attributed = not mentions
            command_position = True
            for tok in tokens:
                top = stack[-1]
                if command_position and tok == "if":
                    stack.append(_ShBlock("if", scope, ln.start, branches=1))
                    if not attributed:
                        stack[-1].test |= mentions
                        attributed = True
                elif command_position and tok == "elif" and top.kind == "if":
                    top.branches += 1
                    if not attributed:
                        top.test |= mentions
                        attributed = True
                elif command_position and tok == "else" and top.kind == "if":
                    top.branches += 1
                    top.has_default = True
                elif command_position and tok == "case":
                    stack.append(_ShBlock("case", scope, ln.start))
                    if not attributed:
                        stack[-1].test |= mentions
                        attributed = True
                elif tok == "in" and top.kind == "case" and not top.in_patterns:
                    top.in_patterns = True
                elif command_position and top.kind in _SH_CLOSES.get(tok, ()):
                    if not attributed:
                        top.rest |= mentions
                        attributed = True
                    self._close(stack.pop(), stack, ln.end)
                elif command_position and tok == "{":
                    if pending_function:
                        stack.append(_ShBlock("function", pending_function, ln.start))
                        pending_function = None
                    else:
                        stack.append(_ShBlock("group", scope, ln.start))
                command_position = tok in _SH_CMD_RESET or (
                    command_position and tok in ("if", "elif", "while", "until"))
            if not attributed:
                stack[-1].rest |= mentions
            self._scan_heredocs(ln, scope)

        last = max(len(self.lines), 1)
        while stack:
            self._close(stack.pop(), stack, last)
        return self.hits

    def _scan_heredocs(self, ln: _ShLine, scope: str) -> None:
        for doc in ln.heredocs:
            prefix = f"{scope}/<<{doc.tag}"
            if not doc.python:
                self._scan_text(doc.body, first_line=doc.start_line, scope=prefix)
                continue
            variants = [doc.body]
            if doc.expands:  # the shell resolves escapes and expansions first
                variants = [shell_expanded(doc.body, escapable=_HEREDOC_ESCAPABLE, expand=False),
                            shell_expanded(doc.body, escapable=_HEREDOC_ESCAPABLE, expand=True),
                            doc.body]
            self._scan_python_program(variants, first_line=doc.start_line, scope=prefix,
                                      what=f"heredoc <<{doc.tag}")

    def _scan_python_c(self, ln: _ShLine, scope: str) -> frozenset[int]:
        """Scan each ``python3 -c '<program>'`` on the line with the Python rules;
        return the indexes of the quoted regions that were programs."""
        skeleton, marks = ln.skeleton_and_marks()
        programs: set[int] = set()
        for k, mark in enumerate(marks):
            if not _PYTHON_C_RE.search(skeleton, 0, mark):
                continue
            s, e = ln.quoted_spans[k]
            raw = ln.code[s:e]
            if len(raw) < 2 or raw[0] not in "'\"" or raw[-1] != raw[0]:
                continue  # $'...' or an unterminated quote: left to the line rules
            body = raw[1:-1]
            if raw[0] == "'":
                variants = [body]
            else:
                variants = [shell_expanded(body, escapable=_DQ_ESCAPABLE, expand=False),
                            shell_expanded(body, escapable=_DQ_ESCAPABLE, expand=True)]
            first_line = ln.quoted_lines[k] if k < len(ln.quoted_lines) else ln.start
            self._scan_python_program(variants, first_line=first_line,
                                      scope=f"{scope}/python-c", what="python -c program")
            programs.add(k)
        return frozenset(programs)

    def _scan_python_program(self, variants: Sequence[str], *, first_line: int, scope: str,
                             what: str) -> None:
        """Scan an embedded program with the Python rules, trying each rendering
        in turn; one that never parses is a scan error (the gate cannot see
        inside it), and its lines still get the text checks."""
        first_error: Optional[BaseException] = None
        for source in dict.fromkeys(variants):
            try:
                self.hits.extend(scan_python_source(self.path, source, self.matcher,
                                                    line_offset=first_line - 1, scope_prefix=scope))
                return
            except (SyntaxError, ValueError) as exc:
                first_error = first_error or exc
        detail = (f"{first_error.msg}, program line {first_error.lineno}"
                  if isinstance(first_error, SyntaxError) else str(first_error))
        self.errors.append(
            f"{self.path}:{first_line}: cannot parse the {what} in {scope} ({detail}), so this "
            "scan cannot see inside it. Make it valid Python as written (quote a heredoc's "
            "delimiter, <<'PY', so the shell leaves it alone) or move it into a .py file.")
        self._scan_text(variants[0], first_line=first_line, scope=scope)

    def _scan_text(self, body: str, *, first_line: int, scope: str) -> None:
        """Line by line: a line that lists the sites by itself (``<a>|<b>``,
        ``<a> <b>``, ``IN ('<a>','<b>')``, ``<a>,<b>``) is a ``string`` hit."""
        for k, raw in enumerate(body.splitlines()):
            lineno = first_line + k
            mentions, enumerates = self.matcher.classify_literal(raw, lineno)
            if enumerates:
                self._hit("string", mentions, scope=scope, size=1, line=lineno, end_line=lineno)


def scan_shell_source(path: str, text: str, matcher: SiteMatcher, *,
                      errors: Optional[list[str]] = None) -> list[Hit]:
    """``errors`` (optional) collects embedded python programs that do not parse."""
    scanner = ShellScanner(path=path, text=text, matcher=matcher)
    hits = scanner.scan()
    if errors is not None:
        errors.extend(scanner.errors)
    return hits


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------

_JS_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
#: Previous significant tokens after which a ``/`` starts a regex literal.
_JS_REGEX_PREV = frozenset({
    "", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-", "*", "%",
    "<", ">", "~", "^", "=>", "&&", "||", "??", "==", "!=", "<=", ">=", "return", "typeof",
    "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield", "await",
})
#: Previous significant tokens after which ``{`` opens an object literal.
_JS_OBJECT_PREV = frozenset({"=", ":", "(", ",", "[", "?", "||", "&&", "??", "return", "{",
                             "default", "=="})
_JS_DECL_WORDS = frozenset({"const", "let", "var", "function", "class", "interface", "type", "enum"})
_JS_CLOSERS = {")": "(", "]": "[", "}": "{"}


@dataclass
class _JsRegion:
    opener: str
    start: int
    scope: str
    object_like: bool
    mentions: set = field(default_factory=set)
    items: int = 0
    expect_item: bool = True


class JsScanner:
    """Bracket-region scan of JS/TS (see the module docstring)."""

    def __init__(self, *, path: str, text: str, matcher: SiteMatcher):
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        self.matcher = matcher
        self.hits: list[Hit] = []
        self.regions: list[_JsRegion] = []
        self.top: dict[str, set] = {}
        self.decl = "<module>"
        self.expect_decl_name = False
        self.prev = ""  # previous significant token

    def _hit(self, kind: str, mentions: Iterable[Mention], *, scope: str, size: int,
             line: int, end_line: int) -> None:
        self.hits.append(_make_hit(path=self.path, scope=scope, kind=kind, size=size,
                                   mentions=mentions, line=line, end_line=end_line,
                                   snippet=_snippet(self.lines, line)))

    def _scope(self) -> str:
        return self.regions[0].scope if self.regions else self.decl

    def _content(self) -> None:
        if self.regions and self.regions[-1].expect_item:
            self.regions[-1].items += 1
            self.regions[-1].expect_item = False

    def _add(self, mentions: set[Mention]) -> None:
        if not mentions:
            return
        if self.regions:
            self.regions[-1].mentions |= mentions
        else:
            self.top.setdefault(self.decl, set()).update(mentions)

    def _literal(self, value: str, line: int) -> None:
        self._content()
        mentions, enumerates = self.matcher.classify_literal(value, line)
        if enumerates:
            self._hit("string", mentions, scope=self._scope(), size=1, line=line, end_line=line)
        else:
            self._add(mentions)

    def _open(self, ch: str, line: int) -> None:
        self._content()
        object_like = ch != "{" or self.prev in _JS_OBJECT_PREV
        self.regions.append(_JsRegion(ch, line, self._scope(), object_like))

    def _close(self, ch: str, line: int) -> None:
        opener = _JS_CLOSERS[ch]
        if not any(r.opener == opener for r in self.regions):
            return  # stray closer (e.g. inside JSX text): ignore
        while self.regions[-1].opener != opener:
            self._finish(self.regions.pop(), line)
        self._finish(self.regions.pop(), line)

    def _finish(self, region: _JsRegion, line: int) -> None:
        if len(_sites_of(region.mentions)) >= 2:
            if region.opener == "[":
                kind = "array"
            elif region.opener == "(":
                kind = "parens"
            else:
                kind = "object" if region.object_like else "block"
            self._hit(kind, region.mentions, scope=region.scope,
                      size=0 if kind == "block" else region.items,
                      line=region.start, end_line=line)
            return
        self._add(region.mentions)

    def _skip_regex(self, i: int, line: int) -> int:
        text, n = self.text, len(self.text)
        j, in_class = i + 1, False
        while j < n and text[j] != "\n":
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                break
            j += 1
        body = text[i + 1:j]
        if self.matcher.is_alternation(body):
            mentions, _ = self.matcher.classify_literal(body, line)
            self._hit("string", mentions, scope=self._scope(), size=1, line=line, end_line=line)
        else:
            self._content()
        j += 1
        while j < n and text[j].isalpha():  # flags
            j += 1
        return j

    def scan(self) -> list[Hit]:
        text, n = self.text, len(self.text)
        i, line = 0, 1
        template_depths: list[int] = []  # brace depth at each open `${`
        brace_depth = 0
        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""
            if ch == "\n":
                line += 1
                i += 1
                continue
            if ch.isspace():
                i += 1
                continue
            if ch == "/" and nxt == "/":
                nl = text.find("\n", i)
                i = n if nl < 0 else nl
                continue
            if ch == "/" and nxt == "*":
                end = text.find("*/", i + 2)
                end = n if end < 0 else end + 2
                line += text.count("\n", i, end)
                i = end
                continue
            if ch in "'\"":
                j, buf = i + 1, []
                while j < n and text[j] != ch and text[j] != "\n":
                    if text[j] == "\\" and j + 1 < n:
                        buf.append(text[j + 1])
                        j += 2
                        continue
                    buf.append(text[j])
                    j += 1
                self._literal("".join(buf), line)
                self.prev = "str"
                i = j + 1 if j < n and text[j] == ch else j
                continue
            if ch == "`" or (ch == "}" and template_depths and template_depths[-1] == brace_depth):
                if ch == "}":
                    template_depths.pop()
                j, buf, start_line = i + 1, [], line
                while j < n and text[j] != "`":
                    if text[j] == "\\" and j + 1 < n:
                        buf.append(text[j + 1])
                        j += 2
                        continue
                    if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
                        break
                    if text[j] == "\n":
                        line += 1
                    buf.append(text[j])
                    j += 1
                self._literal("".join(buf), start_line)
                if j < n and text[j] == "$":
                    template_depths.append(brace_depth)
                    i = j + 2
                    self.prev = "("
                else:
                    i = j + 1
                    self.prev = "str"
                continue
            if ch == "/" and nxt != ">" and self.prev in _JS_REGEX_PREV \
                    and not (i > 0 and text[i - 1] == "<"):
                i = self._skip_regex(i, line)
                self.prev = "str"
                continue
            m = _JS_IDENT_RE.match(text, i)
            if m:
                word = m.group(0)
                if not self.regions:
                    if self.expect_decl_name and word not in _JS_DECL_WORDS:
                        self.decl = word
                        self.expect_decl_name = False
                    elif word in _JS_DECL_WORDS:
                        self.expect_decl_name = True
                    elif word == "default" and self.prev == "export":
                        self.decl = "default"
                self._content()
                self._add(self.matcher.mentions_in_identifier(word, line))
                self.prev = word
                i = m.end()
                continue
            if ch in "([{":
                brace_depth += ch == "{"
                self._open(ch, line)
                self.prev = ch
                i += 1
                continue
            if ch in ")]}":
                brace_depth -= ch == "}"
                self._close(ch, line)
                self.prev = ch
                i += 1
                continue
            if ch == ",":
                if self.regions:
                    self.regions[-1].expect_item = True
                self.prev = ","
                i += 1
                continue
            two = text[i:i + 2]
            if two in ("=>", "&&", "||", "??", "==", "!=", "<=", ">="):
                self.prev = two
                i += 2
                continue
            if ch.isdigit():
                j = i + 1
                while j < n and (text[j].isalnum() or text[j] in "._"):
                    j += 1
                self._content()
                self.prev = "num"
                i = j
                continue
            if ch == ";" and not self.regions:
                self.expect_decl_name = False
            self.prev = ch
            i += 1
        while self.regions:
            self._finish(self.regions.pop(), line)
        for scope, mentions in self.top.items():
            if len(_sites_of(mentions)) >= 2:
                lines = sorted(m.line for m in mentions)
                self._hit("block", mentions, scope=scope, size=0, line=lines[0], end_line=lines[-1])
        return self.hits


def scan_js_source(path: str, text: str, matcher: SiteMatcher) -> list[Hit]:
    return JsScanner(path=path, text=text, matcher=matcher).scan()


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    hits: list[Hit]
    files: dict[str, int]  # language -> number of files scanned
    errors: list[str]
    sanctioned_hits: list[Hit]


def is_test_file(name: str) -> bool:
    return bool(_TEST_FILE_RE.match(name))


def iter_source_files(root: Path, scan_roots: Sequence[str] = SCAN_ROOTS) -> Iterator[Path]:
    for base in scan_roots:
        top = root / base
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
            for name in sorted(filenames):
                if is_test_file(name):
                    continue
                suffix = os.path.splitext(name)[1]
                if suffix in PY_SUFFIXES or suffix in SHELL_SUFFIXES or suffix in JS_SUFFIXES:
                    yield Path(dirpath) / name


def scan_file(path: Path, rel: str, matcher: SiteMatcher, *,
              errors: Optional[list[str]] = None) -> tuple[str, list[Hit]]:
    """``errors`` (optional) collects parts of the file the scan cannot see inside."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix in PY_SUFFIXES:
        return "python", scan_python_source(rel, text, matcher)
    if path.suffix in SHELL_SUFFIXES:
        return "shell", scan_shell_source(rel, text, matcher, errors=errors)
    return "js", scan_js_source(rel, text, matcher)


def scan_tree(root: Path, matcher: SiteMatcher, *, sanctioned: Sequence[str] = (),
              scan_roots: Sequence[str] = SCAN_ROOTS) -> ScanResult:
    hits: list[Hit] = []
    sanctioned_hits: list[Hit] = []
    files: Counter = Counter()
    errors: list[str] = []
    for path in iter_source_files(root, scan_roots):
        rel = path.relative_to(root).as_posix()
        try:
            lang, found = scan_file(path, rel, matcher, errors=errors)
        except (SyntaxError, ValueError) as exc:
            errors.append(f"{rel}: cannot parse ({exc})")
            continue
        files[lang] += 1
        if any(fnmatchcase(rel, pattern) for pattern in sanctioned):
            sanctioned_hits.extend(found)
        else:
            hits.extend(found)
    hits.sort(key=lambda h: (h.path, h.line, h.kind))
    return ScanResult(hits=hits, files=dict(files), errors=errors, sanctioned_hits=sanctioned_hits)


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Allowlist:
    reference_sites: tuple[str, ...]
    entries: tuple[dict, ...]
    sanctioned_paths: tuple[dict, ...]
    not_detected: tuple[dict, ...]

    @property
    def sanctioned_globs(self) -> tuple[str, ...]:
        return tuple(item["path"] for item in self.sanctioned_paths)

    def counts(self) -> Counter:
        allowed: Counter = Counter()
        for entry in self.entries:
            allowed[entry_key(entry)] += int(entry.get("count", 1))
        return allowed


def entry_key(entry: dict) -> tuple[str, str, str, int, tuple[str, ...]]:
    return (entry["path"], entry["scope"], entry["kind"], int(entry["size"]),
            tuple(entry["tokens"]))


_MISSING_ALLOWLIST_HINT = (
    "{path} is missing. Note that the repo-wide `data/` rule in .gitignore (meant for per-agent "
    "runtime data) also matches framework/tests/data/, so a plain `git add` skips this file: "
    "commit it with `git add -f`.")


def read_allowlist_doc(path: Path = ALLOWLIST_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(_MISSING_ALLOWLIST_HINT.format(path=path))
    return json.loads(path.read_text(encoding="utf-8"))


def load_allowlist(path: Path = ALLOWLIST_PATH) -> Allowlist:
    doc = read_allowlist_doc(path)
    return Allowlist(
        reference_sites=tuple(doc.get("reference_sites") or ()),
        entries=tuple(doc.get("entries") or ()),
        sanctioned_paths=tuple(doc.get("sanctioned_paths") or ()),
        not_detected=tuple(doc.get("not_detected") or ()),
    )


def allowlist_problems(doc: dict, repo_root: Path = REPO_ROOT) -> list[str]:
    """Schema and hygiene checks for the allowlist document."""
    problems: list[str] = []
    if doc.get("schema_version") != ALLOWLIST_SCHEMA_VERSION:
        problems.append(f"schema_version must be {ALLOWLIST_SCHEMA_VERSION}")
    sites = doc.get("reference_sites")
    if not (isinstance(sites, list) and len(sites) >= 2
            and all(isinstance(s, str) and s.strip() for s in sites)):
        problems.append("reference_sites must list at least two site ids")
    seen: set = set()
    for n, entry in enumerate(doc.get("entries") or []):
        where = f"entries[{n}]"
        missing = [f for f in _ENTRY_FIELDS if f not in entry]
        if missing:
            problems.append(f"{where}: missing {missing}")
            continue
        extra = sorted(set(entry) - set(_ENTRY_FIELDS))
        if extra:
            problems.append(f"{where}: unknown fields {extra}")
        tokens = entry["tokens"]
        if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
            problems.append(f"{where}: tokens must be a list of strings")
            continue
        if tokens != sorted(set(tokens)):
            problems.append(f"{where}: tokens must be sorted and unique")
        if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0:
            problems.append(f"{where}: size must be a non-negative int")
        if not isinstance(entry["count"], int) or isinstance(entry["count"], bool) or entry["count"] < 1:
            problems.append(f"{where}: count must be an int >= 1")
        if not str(entry["w1"]).strip():
            problems.append(f"{where} ({entry['path']} {entry['scope']}): empty w1 note; say what "
                            "W1 replaces it with, or why it stays")
        if not (repo_root / entry["path"]).is_file():
            problems.append(f"{where}: path {entry['path']!r} does not exist")
        key = entry_key(entry)
        if key in seen:
            problems.append(f"{where}: duplicate key {key[:4]}; merge into one entry with a count")
        seen.add(key)
    for n, item in enumerate(doc.get("sanctioned_paths") or []):
        if not str(item.get("path", "")).strip() or not str(item.get("why", "")).strip():
            problems.append(f"sanctioned_paths[{n}]: needs both 'path' and 'why'")
    for n, item in enumerate(doc.get("not_detected") or []):
        for name in ("path", "where", "why_not_detected", "w1"):
            if not str(item.get(name, "")).strip():
                problems.append(f"not_detected[{n}]: missing {name!r}")
        if item.get("path") and not (repo_root / item["path"]).is_file():
            problems.append(f"not_detected[{n}]: path {item['path']!r} does not exist")
    return problems


def excess_hits(hits: Sequence[Hit], allowed: Counter) -> list[Hit]:
    """Hits beyond what the allowlist covers (count-aware, line-agnostic)."""
    remaining = Counter(allowed)
    out: list[Hit] = []
    for hit in hits:
        if remaining[hit.key] > 0:
            remaining[hit.key] -= 1
        else:
            out.append(hit)
    return out


def stale_entries(hits: Sequence[Hit], allowlist: Allowlist) -> list[tuple[dict, int]]:
    found = Counter(h.key for h in hits)
    out = []
    for entry in allowlist.entries:
        missing = int(entry.get("count", 1)) - found.get(entry_key(entry), 0)
        if missing > 0:
            out.append((entry, missing))
    return out


def format_hit(hit: Hit) -> str:
    return (f"  {hit.location()}  [{hit.kind}, size {hit.size}]  scope={hit.scope}\n"
            f"      {hit.snippet}\n"
            f"      names: {', '.join(hit.tokens)}")


# ---------------------------------------------------------------------------
# Tests against the tree
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def allowlist() -> Allowlist:
    return load_allowlist()  # a missing file raises with the .gitignore hint


@pytest.fixture(scope="module")
def tree_scan(allowlist: Allowlist) -> ScanResult:
    matcher = SiteMatcher(allowlist.reference_sites)
    return scan_tree(REPO_ROOT, matcher, sanctioned=allowlist.sanctioned_globs)


def test_allowlist_file_is_well_formed(allowlist: Allowlist):
    problems = allowlist_problems(read_allowlist_doc())
    assert not problems, "allowlist problems:\n  " + "\n  ".join(problems)


def test_scan_covers_the_tree(tree_scan: ScanResult):
    assert not tree_scan.errors, ("code this scan cannot parse, and so cannot check:\n  "
                                  + "\n  ".join(tree_scan.errors))
    # Guards against a path or filter bug quietly turning the gate into a no-op.
    assert tree_scan.files.get("python", 0) >= 100, tree_scan.files
    assert tree_scan.files.get("shell", 0) >= 1, tree_scan.files


def test_no_new_two_site_enumerations(tree_scan: ScanResult, allowlist: Allowlist):
    new = excess_hits(tree_scan.hits, allowlist.counts())
    if new:
        sites = ", ".join(allowlist.reference_sites)
        pytest.fail(
            f"{len(new)} new code construct(s) enumerate the reference sites ({sites}).\n"
            "Per-site behaviour must come from config (site registry, storage config, site.yaml),\n"
            "never from tuples, dicts or if-chains that list the sites (CLAUDE.md: 'Site-name\n"
            "literals in framework code').\n\n"
            + "\n".join(format_hit(h) for h in new)
            + "\n\nLift the per-site values into config. If this is a deliberate, reviewed exception,\n"
              "add the entry printed by `python3 framework/tests/test_no_two_site_enumerations.py --new`\n"
              f"to {ALLOWLIST_PATH.relative_to(REPO_ROOT)} and fill in its w1 note.",
            pytrace=False)


def test_allowlist_has_no_stale_entries(tree_scan: ScanResult, allowlist: Allowlist):
    stale = stale_entries(tree_scan.hits, allowlist)
    if stale:
        lines = [f"  {e['path']}:{e['line']}  [{e['kind']}] scope={e['scope']}  "
                 f"(missing {missing} of {e['count']})" for e, missing in stale]
        pytest.fail(
            "allowlist entries no longer match any code: the enumeration was removed or changed.\n"
            "Delete or regenerate these entries so the W1 inventory stays exact:\n"
            + "\n".join(lines), pytrace=False)


# ---------------------------------------------------------------------------
# Detector unit tests (synthetic site ids, so they never drift)
# ---------------------------------------------------------------------------

_SYN = SiteMatcher(("acme", "globex"))


def _py(src: str) -> list[tuple[str, int, tuple[str, ...]]]:
    return [(h.kind, h.size, h.tokens) for h in scan_python_source("x.py", src, _SYN)]


@pytest.mark.parametrize("src, expected", [
    ('SITES = ("acme", "globex")\n', [("tuple", 2, ("acme", "globex"))]),
    ('X = ["acme-seo-agent", "globex-seo-agent", "other"]\n',
     [("list", 3, ("acme-seo-agent", "globex-seo-agent"))]),
    ('DSN = {"acme": "ACME_DSN", "globex": "GLOBEX_DSN"}\n',
     [("dict", 2, ("ACME_DSN", "GLOBEX_DSN", "acme", "globex"))]),
    ('d = "acme.com" if site == "acme" else "globex.com"\n',
     [("ifexp", 2, ("acme", "acme.com", "globex.com"))]),
    ('p = "/kitchen" if site == "acme" else "/products"\n',
     [("ifexp:implicit-other", 2, ("acme",))]),
    ('if site == "acme":\n    a = 1\nelif site == "globex":\n    a = 2\n',
     [("if-chain", 2, ("acme", "globex"))]),
    ('if site == "acme":\n    q = 1\nelse:\n    q = 2\n',
     [("if-chain:implicit-other", 2, ("acme",))]),
    ('match site:\n    case "acme":\n        x = 1\n    case "globex":\n        x = 2\n',
     [("match", 2, ("acme", "globex"))]),
    ('match site:\n    case "acme":\n        x = 1\n    case _:\n        x = 2\n',
     [("match:implicit-other", 2, ("acme",))]),
    ('raise SystemExit("set SITE=<acme|globex>")\n',
     [("string", 1, ("set SITE=<acme|globex>",))]),
    ('PAT = r"^(acme|globex)-"\n', [("string", 1, ("^(acme|globex)-",))]),
    ('DEFAULT = "acme,globex"\n', [("string", 1, ("acme,globex",))]),
    ('ok = os.environ.get("ACME_DSN") or os.environ.get("GLOBEX_DSN")\n',
     [("boolop", 2, ("ACME_DSN", "GLOBEX_DSN"))]),
    ('def f():\n    a = load("acme")\n    b = load("globex")\n',
     [("block", 0, ("acme", "globex"))]),
    ('x = dict(acme=1, globex=2)\n', [("call", 2, ("acme", "globex"))]),
    ('if is_acme(x):\n    y = 1\nelif GLOBEX_MODE:\n    y = 2\n',
     [("if-chain", 2, ("GLOBEX_MODE", "is_acme"))]),
    # A def's header belongs to the def (the review's render_link_directive shape).
    ('def f(root="https://acme.com"):\n    return root + "?tag=globex-20"\n',
     [("block", 0, ("?tag=globex-20", "https://acme.com"))]),
    ('@register("acme")\nclass A:\n    X = "globex"\n', [("block", 0, ("acme", "globex"))]),
    # Strings that list the sites by themselves, whitespace or not.
    ('SITES = "acme globex".split()\n', [("string", 1, ("acme globex",))]),
    ('S = "acme, globex"\n', [("string", 1, ("acme, globex",))]),
    ('Q = "DELETE FROM t WHERE site IN (\'acme\', \'globex\')"\n',
     [("string", 1, ("DELETE FROM t WHERE site IN ('acme', 'globex')",))]),
    ('J = \'{"acme": 1, "globex": 2}\'\n', [("string", 1, ('{"acme": 1, "globex": 2}',))]),
])
def test_python_detector_flags(src, expected):
    assert _py(src) == expected


@pytest.mark.parametrize("src", [
    '# SITES = ("acme", "globex")\nx = 1\n',
    'def f():\n    """Works for acme and globex: ("acme", "globex")."""\n    return 1\n',
    'log.info("refreshing acme and then globex catalogs")\n',
    'X = {"acme": 1}\nY = 2\n',
    'def a():\n    return "acme"\n\ndef b():\n    return "globex"\n',
    'if site == "acme":\n    extra()\n',
    'help_text = "e.g. acme, globex"\n',
    'x = "/kitchen" if flag else "/products"\n',
    'help_text = "the app name (e.g., \'Acme\', \'Globex\')"\n',  # display names, not ids
    'msg = "acme then globex"\n',
    'def f(x="acme"):\n    return x\n',
])
def test_python_detector_ignores(src):
    assert _py(src) == []


def test_def_header_belongs_to_the_def():
    src = 'def f(root="https://acme.com"):\n    return root + "?tag=globex-20"\n'
    (hit,) = scan_python_source("x.py", src, _SYN)
    assert (hit.scope, hit.kind, hit.line, hit.end_line) == ("f", "block", 1, 2)
    # A header mention that forms no hit in the def still counts where the def sits.
    (hit,) = scan_python_source("x.py", 'SITE = "acme"\n\ndef f(x="globex"):\n    return x\n', _SYN)
    assert (hit.scope, hit.kind) == ("<module>", "block")
    # A container in the header is evaluated, and reported, in the enclosing scope.
    src = 'class A:\n    def f(self, s=("acme", "globex")):\n        return s\n'
    (hit,) = scan_python_source("x.py", src, _SYN)
    assert (hit.scope, hit.kind) == ("A", "tuple")


def test_python_innermost_construct_consumes_mentions():
    src = 'CFG = {"acme": ("acme", "globex"), "globex": {"x": 1}}\n'
    # The inner tuple enumerates; the outer dict's own keys enumerate again.
    assert [(h.kind, h.size) for h in scan_python_source("x.py", src, _SYN)] == [
        ("tuple", 2), ("dict", 2)]
    nested = 'ROWS = [("acme", "a.com"), ("globex", "g.com")]\n'
    assert _py(nested) == [("list", 2, ("acme", "globex"))]


def test_keys_are_line_agnostic_but_arity_sensitive():
    base = 'def f():\n    return ("acme", "globex")\n'
    moved = "\n\n\n# comment\n" + base
    grown = 'def f():\n    return ("acme", "globex", "initech")\n'
    (k,) = scan_python_source("x.py", base, _SYN)
    (m,) = scan_python_source("x.py", moved, _SYN)
    (g,) = scan_python_source("x.py", grown, _SYN)
    assert m.key == k.key and m.line == k.line + 4
    assert g.key != k.key
    assert k.scope == "f"


def test_scopes_are_qualified():
    src = 'class A:\n    def run(self):\n        return {"acme": 1, "globex": 2}\n'
    (hit,) = scan_python_source("x.py", src, _SYN)
    assert (hit.scope, hit.kind, hit.line) == ("A.run", "dict", 3)


def _sh(src: str) -> list[tuple[str, str, int, tuple[str, ...]]]:
    return [(h.kind, h.scope, h.size, h.tokens) for h in scan_shell_source("x.sh", src, _SYN)]


_SHELL_SAMPLE = """\
#!/usr/bin/env bash
# for s in acme globex   (comment only)
echo "migrating acme and globex"   # prose, then a comment
ACME_DSN="${ACME_DSN:-}" \\
    GLOBEX_DSN="${GLOBEX_DSN:-}" \\
    python3 reconcile.py
pick() {
  case "$1" in
    acme) echo a ;;
    globex) echo b ;;
  esac
}
if [ "$SITE" = "acme" ]; then
  X=1
else
  X=2
fi
SITES=(
  acme
  globex
)
python3 - <<'PY'
HOSTS = {'acme.com', 'globex.com'}
PY
cat <<EOF
acme and globex are mentioned here as prose
EOF
grep -E '^(acme|globex)-' list.txt
"""


def test_shell_detector():
    assert _sh(_SHELL_SAMPLE) == [
        ("line", "<script>", 0, ("ACME_DSN", "GLOBEX_DSN")),
        ("case", "pick", 2, ("acme", "globex")),
        ("if-chain:implicit-other", "<script>", 2, ("acme",)),
        ("array", "<script>", 0, ("acme", "globex")),
        ("set", "<script>/<<PY", 2, ("acme.com", "globex.com")),
        ("string", "<script>", 1, ("^(acme|globex)-",)),
    ]
    hits = scan_shell_source("x.sh", _SHELL_SAMPLE, _SYN)
    assert [(h.line, h.end_line) for h in hits] == [
        (4, 6), (8, 11), (13, 17), (18, 21), (23, 23), (28, 28)]


def test_shell_comments_quotes_and_heredocs_do_not_leak():
    src = (
        "X='# not a comment acme'\n"
        "Y=\"$(echo \"globex\")\"  # acme globex\n"
        "cat <<'EOF'\n"
        "if acme; then globex; fi\n"
        "EOF\n"
        "echo ${#ARR[@]}  # globex\n"
        "msg=\"deploying acme, then globex\"\n"
    )
    assert _sh(src) == []


def test_shell_function_bodies_are_boundaries():
    src = "a() {\n  run acme\n}\nb() {\n  run globex\n}\nc() {\n  run acme\n  run globex\n}\n"
    assert _sh(src) == [("block", "c", 0, ("acme", "globex"))]


_EMBEDDED_SAMPLE = """\
run() {
  python3 -c 'S = ("acme", "globex"); print(S)'
  X=$(python3 -u -c "import os; print({\\"acme\\": os.environ['ACME_DSN'], \\"globex\\": 1})")
  python3 -c "print($ACME_DSN, '$GLOBEX_DSN')"
  python3 -c 'print("acme")'
}
\tpython3 - <<-PY
\tS = {'acme', 'globex'}
\tPY
python3 - <<EOF
n = $COUNT
S = ('acme', 'globex')
EOF
psql "$DSN" <<'SQL'
DELETE FROM t WHERE site IN ('acme', 'globex');
SQL
SITES="acme globex"
msg="deploying acme, then globex"
"""


def test_shell_embedded_python_and_inline_lists():
    errors: list[str] = []
    hits = scan_shell_source("x.sh", _EMBEDDED_SAMPLE, _SYN, errors=errors)
    assert errors == []
    assert sorted((h.line, h.kind, h.scope, h.size, h.tokens) for h in hits) == [
        # python -c: single-quoted, double-quoted with \" escapes, and with
        # expansions that only parse once the shell has replaced them.
        (2, "tuple", "run/python-c", 2, ("acme", "globex")),
        (3, "dict", "run/python-c", 2, ("ACME_DSN", "acme", "globex")),
        (4, "call", "run/python-c", 2, ("ACME_DSN", "GLOBEX_DSN")),
        (8, "set", "<script>/<<PY", 2, ("acme", "globex")),       # <<- strips the tabs
        (12, "tuple", "<script>/<<EOF", 2, ("acme", "globex")),   # unquoted: $COUNT expands
        (15, "string", "<script>/<<SQL", 1, ("DELETE FROM t WHERE site IN ('acme', 'globex');",)),
        (17, "string", "<script>", 1, ("acme globex",)),
    ]


def test_shell_unparseable_python_is_a_scan_error():
    src = "go() {\n  python3 - <<'PY'\nS = ('acme', 'globex'\nPY\n  python3 -c 'if:'\n}\n"
    errors: list[str] = []
    hits = scan_shell_source("x.sh", src, _SYN, errors=errors)
    assert len(errors) == 2, errors
    assert errors[0].startswith("x.sh:3: cannot parse the heredoc <<PY in go/<<PY (")
    assert errors[1].startswith("x.sh:5: cannot parse the python -c program in go/python-c (")
    # The text checks still run on what could not be parsed.
    assert [(h.kind, h.scope, h.line) for h in hits] == [("string", "go/<<PY", 3)]


def test_shell_expanded_renders_what_python_sees():
    dq = 'print(\\"x\\", $A, ${B:-1}, $(cmd $(sub)), `cmd`, $1, \\$C)'
    assert shell_expanded(dq, escapable=_DQ_ESCAPABLE, expand=True) == (
        'print("x", A, B, _sh_, _sh_, _sh_, $C)')
    assert shell_expanded(dq, escapable=_DQ_ESCAPABLE, expand=False) == (
        'print("x", $A, ${B:-1}, $(cmd $(sub)), `cmd`, $1, $C)')
    assert shell_expanded("a = 1 \\\n  + 2  # \\\"q\\\"", escapable=_HEREDOC_ESCAPABLE,
                          expand=False) == 'a = 1   + 2  # \\"q\\"'


def _js(src: str) -> list[tuple[str, str, int, tuple[str, ...]]]:
    return [(h.kind, h.scope, h.size, h.tokens) for h in scan_js_source("x.tsx", src, _SYN)]


_JS_SAMPLE = """\
// const S = ['acme', 'globex']
/* {'acme': 1, 'globex': 2} */
const EMOJI: Record<string, string> = {
  'all': '*',
  'acme': 'A',
  globex: 'G',
}
export function label(site: string) {
  const re = /^(acme|globex)-/
  return site === 'acme' ? 'Acme' : `Globex ${site}`
}
const msg = `deploying acme then globex`
const a = 1 / 2; const b = 'acme'
const el = <div className="x">Don't panic</div>
const c = 'globex'
"""


def test_js_detector():
    assert _js(_JS_SAMPLE) == [
        ("object", "EMOJI", 3, ("acme", "globex")),
        ("string", "label", 1, ("^(acme|globex)-",)),
        ("block", "label", 0, ("Acme", "Globex", "acme")),
    ]


def test_skip_rules():
    assert is_test_file("test_x.py") and is_test_file("conftest.py")
    assert is_test_file("agents.spec.ts") and is_test_file("x_test.py")
    assert not is_test_file("agent.py") and not is_test_file("testing.py")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_gate_pipeline_on_a_scratch_tree(tmp_path: Path):
    _write(tmp_path, "framework/core/a.py",
           'SITES = ("acme", "globex")\n\ndef f():\n    return ("acme", "globex")\n'
           'def g():\n    x = ("acme", "globex")\n    y = ("acme", "globex")\n')
    _write(tmp_path, "agents/x/agent.py", 'DSN = {"acme": "A", "globex": "G"}\n')
    _write(tmp_path, "agents/x/run.sh", "for s in acme globex; do echo $s; done\n")
    _write(tmp_path, "agents/x/test_agent.py", 'S = ("acme", "globex")\n')       # test file
    _write(tmp_path, "agents/x/tests/helper.py", 'S = ("acme", "globex")\n')     # test dir
    _write(tmp_path, "framework/ui/node_modules/m.js", "const s = ['acme', 'globex']\n")
    _write(tmp_path, "framework/core/registry.py", 'DEFAULTS = ("acme", "globex")\n')
    _write(tmp_path, "docs/notes.py", 'S = ("acme", "globex")\n')                 # outside roots
    _write(tmp_path, "framework/core/broken.py", "def (:\n")

    result = scan_tree(tmp_path, _SYN, sanctioned=["framework/core/registry.py"])
    assert result.errors and "framework/core/broken.py" in result.errors[0]
    assert [(h.path, h.scope, h.kind) for h in result.hits] == [
        ("agents/x/agent.py", "<module>", "dict"),
        ("agents/x/run.sh", "<script>", "line"),
        ("framework/core/a.py", "<module>", "tuple"),
        ("framework/core/a.py", "f", "tuple"),
        ("framework/core/a.py", "g", "tuple"),
        ("framework/core/a.py", "g", "tuple"),
    ]
    assert [h.path for h in result.sanctioned_hits] == ["framework/core/registry.py"]
    assert result.files == {"python": 3, "shell": 1}

    entries = [h.as_entry(w1="known") for h in result.hits[:5]]  # covers g() only once
    allow = Allowlist(reference_sites=("acme", "globex"), entries=tuple(entries),
                      sanctioned_paths=(), not_detected=())
    new = excess_hits(result.hits, allow.counts())
    assert [(h.path, h.scope) for h in new] == [("framework/core/a.py", "g")]

    entries[-1]["count"] = 2
    stale_doc = result.hits[0].as_entry(w1="gone")
    stale_doc["tokens"] = ["acme", "globex", "initech"]
    allow = Allowlist(reference_sites=("acme", "globex"), entries=(*entries, stale_doc),
                      sanctioned_paths=(), not_detected=())
    assert excess_hits(result.hits, allow.counts()) == []
    assert [(e["path"], missing) for e, missing in stale_entries(result.hits, allow)] == [
        ("agents/x/agent.py", 1)]


def test_allowlist_validation_catches_mistakes(tmp_path: Path):
    _write(tmp_path, "agents/x/agent.py", "")
    good = {"path": "agents/x/agent.py", "scope": "<module>", "kind": "tuple", "size": 2,
            "tokens": ["acme", "globex"], "count": 1, "line": 1, "snippet": "", "w1": "W1: registry"}
    doc = {"schema_version": 1, "reference_sites": ["acme", "globex"], "entries": [good]}
    assert allowlist_problems(doc, tmp_path) == []
    bad = dict(doc, reference_sites=["acme"], entries=[
        dict(good, w1=" "),
        dict(good, tokens=["globex", "acme"]),
        dict(good, path="agents/missing.py"),
        dict(good, count=0, extra=1),
        good, good,
    ], sanctioned_paths=[{"path": "x.py"}],
        not_detected=[{"path": "agents/x/agent.py", "where": "w"}])
    problems = "\n".join(allowlist_problems(bad, tmp_path))
    for needle in ("reference_sites", "empty w1 note", "sorted and unique", "does not exist",
                   "count must be", "unknown fields", "duplicate key", "sanctioned_paths[0]",
                   "missing 'why_not_detected'", "missing 'w1'"):
        assert needle in problems, needle


def test_cli_prints_ready_to_paste_entries(capsys):
    assert _main(["--new"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_missing_allowlist_explains_the_gitignore_trap(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="git add -f"):
        load_allowlist(tmp_path / "data" / "absent.json")


# ---------------------------------------------------------------------------
# CLI: print hits as ready-to-paste allowlist entries
# ---------------------------------------------------------------------------

def _main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scan framework/ and agents/ for code that enumerates the reference sites.")
    ap.add_argument("--new", action="store_true", help="only hits the allowlist does not cover")
    ap.add_argument("--text", action="store_true", help="human-readable output instead of JSON")
    args = ap.parse_args(argv)
    allow = load_allowlist()
    result = scan_tree(REPO_ROOT, SiteMatcher(allow.reference_sites),
                       sanctioned=allow.sanctioned_globs)
    hits = excess_hits(result.hits, allow.counts()) if args.new else result.hits
    if args.text:
        for hit in hits:
            print(format_hit(hit))
        print(f"{len(hits)} hit(s); files scanned: {result.files}; "
              f"sanctioned hits: {len(result.sanctioned_hits)}")
    else:
        merged: dict[tuple, dict] = {}
        for hit in hits:
            if hit.key in merged:
                merged[hit.key]["count"] += 1
            else:
                merged[hit.key] = hit.as_entry()
        print(json.dumps(list(merged.values()), indent=2, ensure_ascii=False))
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
