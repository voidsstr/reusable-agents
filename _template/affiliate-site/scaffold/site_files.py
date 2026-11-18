#!/usr/bin/env python3
"""Site files derived from the kit at scaffold time.

    python3 -I site_files.py package    --kit KIT --site SITE
    python3 -I site_files.py deploy-env --kit KIT --site SITE --set KEY=VALUE [--set ...]

package
    Fills the site's package.json dependencies and devDependencies with the
    kit's exact pins (the vendored kit is compiled and tested with exactly
    those), and writes package-lock.json from the kit's lockfile with the root
    entry rewritten for the site. The two then describe the same dependency
    tree the kit was tested on, so `npm ci` works in a fresh repo (and in the
    image build) without a network round trip to resolve versions. A site
    that adds a dependency later runs `npm install <pkg>@<version>
    --save-exact`, which updates both files.

deploy-env
    Writes the site's deploy.env from the kit's deploy/deploy.env.example
    (which documents every key the deploy scripts accept), replacing the
    values of the keys given with --set and keeping every comment. Values are
    double-quoted when they contain a space; shell syntax ($, backquotes,
    quotes, backslashes) is refused, as the deploy scripts refuse it.

Self-contained: runs under `python3 -I`. Exit: 0 ok; 1 failed; 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
ENV_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
BAD_VALUE_RE = re.compile(r"[\"'`$\\\n\r]")
ROOT_FIELDS = ("name", "version", "license", "dependencies", "devDependencies", "engines")


class SiteFilesError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SiteFilesError(f"cannot read {path}: {exc}") from None
    if not isinstance(value, dict):
        raise SiteFilesError(f"{path}: expected a JSON object")
    return value


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def cmd_package(kit: Path, site: Path) -> int:
    kit_pkg = read_json(kit / "package.json")
    kit_lock = read_json(kit / "package-lock.json")
    site_pkg_path = site / "package.json"
    site_pkg = read_json(site_pkg_path)
    if not isinstance(site_pkg.get("name"), str) or not site_pkg["name"]:
        raise SiteFilesError(f"{site_pkg_path}: name is missing")
    if kit_lock.get("lockfileVersion") not in (2, 3):
        raise SiteFilesError(f"the kit's package-lock.json has lockfileVersion {kit_lock.get('lockfileVersion')}; expected 2 or 3")
    deps = dict(kit_pkg.get("dependencies") or {})
    dev = dict(kit_pkg.get("devDependencies") or {})
    extra = sorted(set(site_pkg.get("dependencies") or {}) - set(deps)) + sorted(set(site_pkg.get("devDependencies") or {}) - set(dev))
    if extra:
        raise SiteFilesError(
            "the template's package.json declares dependencies the kit does not ("
            + ", ".join(extra) + "): the lockfile is derived from the kit's, so add them after scaffolding with npm install"
        )
    site_pkg["dependencies"] = deps
    site_pkg["devDependencies"] = dev
    write_json(site_pkg_path, site_pkg)

    packages = kit_lock.get("packages")
    if not isinstance(packages, dict) or "" not in packages:
        raise SiteFilesError("the kit's package-lock.json has no root package entry")
    for name, version in {**deps, **dev}.items():
        got = (packages.get(f"node_modules/{name}") or {}).get("version")
        if got != version:
            raise SiteFilesError(f"the kit's lockfile has {name} {got}, but its package.json pins {version}; fix the kit first")
    # The root entry mirrors the site's package.json for the fields npm copies
    # there; everything else about the kit's root (none today) is dropped.
    root = {k: site_pkg[k] for k in ROOT_FIELDS if k in site_pkg}
    lock: dict = {"name": site_pkg["name"]}
    if "version" in site_pkg:
        lock["version"] = site_pkg["version"]
    for k, v in kit_lock.items():
        if k not in ("name", "version", "packages"):
            lock[k] = v
    lock["packages"] = {"": root, **{k: v for k, v in packages.items() if k != ""}}
    write_json(site / "package-lock.json", lock)
    print(f"package.json and package-lock.json: {len(deps)} dependencies, {len(dev)} devDependencies (the kit's pins)")
    return EXIT_OK


def parse_sets(items: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not re.match(r"^[A-Z][A-Z0-9_]*$", key):
            raise SiteFilesError(f"--set {item!r}: expected KEY=VALUE", EXIT_USAGE)
        if key in out:
            raise SiteFilesError(f"--set {key} given twice", EXIT_USAGE)
        if BAD_VALUE_RE.search(value):
            raise SiteFilesError(f"--set {key}: quotes, backslashes, $ and backquotes are not allowed in deploy.env values",
                                 EXIT_USAGE)
        out[key] = value
    return out


PLAIN_VALUE_RE = re.compile(r"[A-Za-z0-9_@%+=:,./-]*")


def env_value(value: str) -> str:
    """A deploy.env value: as is when it is plain words, else double-quoted.

    The deploy tools' parser (deploytool parse_env_value) refuses unquoted
    whitespace and shell metacharacters (;&|<>() and the rest), so anything
    beyond plain word characters is quoted; $, backquotes, quotes and
    backslashes never get this far (parse_sets refuses them).
    """
    return value if PLAIN_VALUE_RE.fullmatch(value) else f'"{value}"'


def cmd_deploy_env(kit: Path, site: Path, sets: Dict[str, str], header: str) -> int:
    example = kit / "deploy" / "deploy.env.example"
    try:
        lines = example.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SiteFilesError(f"cannot read {example}: {exc}") from None
    seen: Dict[str, int] = {}
    out: List[str] = []
    for line in lines:
        m = ENV_LINE_RE.match(line)
        if m and m.group(1) in sets:
            key = m.group(1)
            seen[key] = seen.get(key, 0) + 1
            out.append(f"{key}={env_value(sets[key])}")
        else:
            out.append(line)
    missing = sorted(set(sets) - set(seen))
    if missing:
        raise SiteFilesError(f"{example} has no {', '.join(missing)} line: the kit's deploy.env contract changed; "
                             "update the scaffold")
    twice = sorted(k for k, n in seen.items() if n > 1)
    if twice:
        raise SiteFilesError(f"{example} sets {', '.join(twice)} more than once")
    text = "\n".join(out).rstrip("\n") + "\n"
    target = site / "deploy.env"
    if target.exists():
        raise SiteFilesError(f"{target} already exists")
    target.write_text(header + text, encoding="utf-8")
    os.chmod(target, 0o644)
    print(f"deploy.env: {len(sets)} values set from the scaffold, the rest as the kit documents them")
    return EXIT_OK


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="site_files.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("package")
    p.add_argument("--kit", required=True)
    p.add_argument("--site", required=True)
    p = sub.add_parser("deploy-env")
    p.add_argument("--kit", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--header-line", action="append", default=[], help="a comment line written above the kit's text")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    kit = Path(args.kit).resolve()
    site = Path(args.site).absolute()
    try:
        if not site.is_dir():
            raise SiteFilesError(f"site directory not found: {site}", EXIT_USAGE)
        if args.cmd == "package":
            return cmd_package(kit, site)
        header = "".join(f"# {line}".rstrip() + "\n" for line in args.header_line) + ("\n" if args.header_line else "")
        return cmd_deploy_env(kit, site, parse_sets(args.set), header)
    except SiteFilesError as exc:
        print(f"site_files: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
