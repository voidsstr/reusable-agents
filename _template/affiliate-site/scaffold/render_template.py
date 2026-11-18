#!/usr/bin/env python3
"""Render the affiliate-site template (../site) into a new directory.

    python3 -I render_template.py --template DIR --out DIR --var NAME=VALUE [--var ...]

Used by install/create-affiliate-site.sh; standalone so it can be tested on
its own. Self-contained on purpose: it runs under `python3 -I` (no user site
packages, no script directory on sys.path), like the kit's deploy tools.

Rules
-----
- Every file under --template is written to the same relative path under
  --out, with two renames per path segment: a leading "dot-" becomes "."
  (dot-gitignore -> .gitignore, so the template's own dotfiles never act on
  the reusable-agents checkout), and "__NICHE__" becomes the NICHE variable.
- A file whose name ends in ".tmpl" is rendered and the suffix dropped; every
  other file is copied byte for byte.
- Placeholders in a .tmpl file:
      {{NAME}}            the value as is
      {{json:NAME}}       a JSON string literal (also a valid YAML double-quoted
                          scalar and a valid TypeScript string literal)
      {{sql:NAME}}        a SQL string literal ('...' with quotes doubled)
      {{jsonlist:NAME}}   a JSON array of the value's comma-separated items
                          (valid JSON, YAML flow sequence and TypeScript)
      {{shq:NAME}}        a single-quoted shell word ('...', a quote inside
                          written as '\\''): one bash word, never expanded
  An unknown NAME or encoder is an error. A bare {{NAME}} whose value holds
  anything but [A-Za-z0-9._:/@+,=-] is an error outside Markdown: free text
  (a brand name with a space or an apostrophe) must go through an encoder, so
  no value can break the syntax of the file it lands in.
- Rendered output may not contain anything that still looks like a
  placeholder.
- Modes are set here, never inherited from the template checkout: *.sh
  0755, everything else 0644 (directories 0755).
- --out must be missing or an empty directory; nothing outside it is written.
- Symbolic links in the template are refused.

Prints one JSON object on stdout: {"files": [<relative paths written>]}.
Exit: 0 rendered; 1 template error; 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List

EXIT_OK, EXIT_TEMPLATE, EXIT_USAGE = 0, 1, 2

VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(?:([a-z]+):)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Anything that still reads like a placeholder after rendering.
LEFTOVER_RE = re.compile(r"\{\{\s*(?:[a-z]+:)?[A-Za-z_][A-Za-z0-9_]*\s*\}\}")
SAFE_RAW_RE = re.compile(r"^[A-Za-z0-9._:/@+,=-]*$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LIST_ITEM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ENCODERS = ("json", "sql", "jsonlist", "shq")
TEMPLATE_SUFFIX = ".tmpl"
DOT_PREFIX = "dot-"
NICHE_TOKEN = "__NICHE__"


class TemplateError(Exception):
    pass


def parse_vars(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not VAR_NAME_RE.match(name):
            raise TemplateError(f"--var {item!r}: expected NAME=VALUE with an upper-case NAME")
        if name in out:
            raise TemplateError(f"--var {name} given twice")
        if CONTROL_RE.search(value) or "\n" in value or "\r" in value:
            raise TemplateError(f"--var {name}: control characters and line breaks are not allowed")
        if "{{" in value or "}}" in value:
            raise TemplateError(f"--var {name}: the value may not contain '{{{{' or '}}}}'")
        out[name] = value
    return out


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shell_word(value: str) -> str:
    """One single-quoted bash word: nothing inside it is expanded."""
    return "'" + value.replace("'", "'\\''") + "'"


def json_list(value: str, name: str) -> str:
    items = [p.strip() for p in value.split(",") if p.strip()]
    for item in items:
        if not LIST_ITEM_RE.match(item):
            raise TemplateError(f"{name}: list item {item!r} must match [A-Za-z0-9_.-]+")
    return json.dumps(items)


def render_text(text: str, variables: Dict[str, str], rel: str) -> str:
    markdown = rel.lower().endswith(".md")

    def sub(m: "re.Match[str]") -> str:
        enc, name = m.group(1), m.group(2)
        if name not in variables:
            raise TemplateError(f"{rel}: unknown placeholder {m.group(0)}")
        value = variables[name]
        if enc is None:
            if not markdown and not SAFE_RAW_RE.match(value):
                raise TemplateError(
                    f"{rel}: {{{{{name}}}}} would insert {value!r} unquoted; use {{{{json:{name}}}}} "
                    f"or {{{{sql:{name}}}}} for free text"
                )
            return value
        if enc == "json":
            return json.dumps(value, ensure_ascii=False)
        if enc == "sql":
            return sql_literal(value)
        if enc == "jsonlist":
            return json_list(value, name)
        if enc == "shq":
            return shell_word(value)
        raise TemplateError(f"{rel}: unknown encoder {enc!r} in {m.group(0)} (known: {', '.join(ENCODERS)})")

    out = PLACEHOLDER_RE.sub(sub, text)
    left = LEFTOVER_RE.search(out)
    if left:
        raise TemplateError(f"{rel}: rendered text still holds {left.group(0)!r}")
    return out


def output_rel(rel: PurePosixPath, variables: Dict[str, str]) -> PurePosixPath:
    parts: List[str] = []
    for seg in rel.parts:
        if NICHE_TOKEN in seg:
            niche = variables.get("NICHE")
            if not niche:
                raise TemplateError(f"{rel}: path needs the NICHE variable")
            seg = seg.replace(NICHE_TOKEN, niche)
        if seg.startswith(DOT_PREFIX) and len(seg) > len(DOT_PREFIX):
            seg = "." + seg[len(DOT_PREFIX):]
        parts.append(seg)
    name = parts[-1]
    if name.endswith(TEMPLATE_SUFFIX):
        parts[-1] = name[: -len(TEMPLATE_SUFFIX)]
    out = PurePosixPath(*parts)
    if any(p in ("", ".", "..") for p in out.parts):
        raise TemplateError(f"{rel}: renders to an unsafe path {out}")
    return out


def template_files(root: Path) -> List[PurePosixPath]:
    found: List[PurePosixPath] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                raise TemplateError(f"symbolic link in the template: {os.path.join(dirpath, d)}")
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                raise TemplateError(f"symbolic link in the template: {full}")
            if not stat.S_ISREG(st.st_mode):
                raise TemplateError(f"not a regular file: {full}")
            if f.endswith((".pyc", "~")) or f == ".DS_Store":
                continue
            found.append(PurePosixPath(os.path.relpath(full, root).replace(os.sep, "/")))
    return found


def render(template: Path, out: Path, variables: Dict[str, str]) -> List[str]:
    if not template.is_dir():
        raise TemplateError(f"template directory not found: {template}")
    if out.exists():
        if not out.is_dir() or any(out.iterdir()):
            raise TemplateError(f"{out} exists and is not an empty directory")
    files = template_files(template)
    if not files:
        raise TemplateError(f"{template} holds no files")
    planned: Dict[PurePosixPath, PurePosixPath] = {}
    for rel in files:
        dest = output_rel(rel, variables)
        if dest in planned.values():
            raise TemplateError(f"two template files render to {dest}")
        planned[rel] = dest
    # Render everything in memory first: a template error writes nothing.
    contents: Dict[PurePosixPath, bytes] = {}
    for rel, dest in planned.items():
        data = (template / rel).read_bytes()
        if rel.name.endswith(TEMPLATE_SUFFIX):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise TemplateError(f"{rel}: a .tmpl file must be UTF-8 text") from None
            data = render_text(text, variables, str(dest)).encode("utf-8")
        contents[dest] = data
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o755)
    written: List[str] = []
    for dest in sorted(contents):
        target = out.joinpath(*dest.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        for p in target.parents:
            if p == out or out not in p.parents:
                break
            os.chmod(p, 0o755)
        target.write_bytes(contents[dest])
        os.chmod(target, 0o755 if dest.name.endswith(".sh") else 0o644)
        written.append(str(dest))
    return written


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Render the affiliate-site template.")
    ap.add_argument("--template", required=True, help="the template's site/ directory")
    ap.add_argument("--out", required=True, help="the directory to create (missing or empty)")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        variables = parse_vars(args.var)
        written = render(Path(args.template).resolve(), Path(args.out).absolute(), variables)
    except TemplateError as exc:
        print(f"render_template: {exc}", file=sys.stderr)
        return EXIT_TEMPLATE
    except OSError as exc:
        print(f"render_template: {exc}", file=sys.stderr)
        return EXIT_TEMPLATE
    json.dump({"files": written}, sys.stdout)
    sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
