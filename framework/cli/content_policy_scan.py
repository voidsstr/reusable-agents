#!/usr/bin/env python3
"""Scan rendered pages (or markdown) against a site's content policy and exit
non-zero on a violation (framework.core.content_policy_audit).

The kit gate (build item K5) runs this over the pages it renders from its
fixture database; KTLO and operators run it against live pages. Which rules
apply: `--rule-set` flags when given (a site's suite.env can name them, so
the gate needs no storage access), else the rule sets the `--site` opted
into in config/content-policy-config.json (storage copy laid over the repo
default; `--config FILE` laid over both).

    # rendered pages on disk (a directory is scanned for *.html / *.htm)
    python3 -m framework.cli.content_policy_scan --rule-set electrical \\
        --rule-set electrical-year1 --rule-set medical out/pages/

    # one page from stdin
    curl -s https://<site>/guides/<slug> | python3 -m framework.cli.content_policy_scan --site <site> --stdin

    # live /guides/* pages from the sitemap (or crawled from --base when the
    # sitemap has none)
    python3 -m framework.cli.content_policy_scan --site <site> --base https://<site> --sitemap \\
        --path-re '^/guides/' --max-pages 300

Network use: GET only, same host only, and never a path matching
--never-fetch-re (default: affiliate click-outs /r/, /k/, /go/ and /api/),
whether it comes from the sitemap, a crawl, --url or a redirect.

Exit codes: 0 every page read and no reject-level violation (warnings too
with --strict); 1 violation; 2 usage or config error (including "no rule sets
apply", unless --allow-empty-policy, and an unreadable storage copy of the
config); 3 a page or file could not be read (unverified content fails a
gate; --allow-errors makes it 0 when at least one page was scanned and none
violated) or nothing was scanned at all.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from framework.core.content_policy import (  # noqa: E402
    ContentPolicyConfigError,
    load_config,
    resolve_policy,
)
from framework.core.content_policy_audit import (  # noqa: E402
    DEFAULT_NEVER_FETCH,
    PageResult,
    ScanReport,
    crawl,
    http_fetcher,
    scan_documents,
    scan_fetched,
    sitemap_urls,
)

EXIT_OK, EXIT_VIOLATION, EXIT_USAGE, EXIT_NOTHING = 0, 1, 2, 3


def _storage():
    try:
        from framework.core.storage import get_storage
        return get_storage()
    except (Exception, SystemExit):  # unknown STORAGE_BACKEND raises SystemExit
        return None


def _fmt_for(path: str, forced: str) -> str:
    if forced != "auto":
        return forced
    return "markdown" if path.lower().endswith((".md", ".markdown")) else "html"


def _file_docs(paths: list[str], forced: str) -> tuple[list[tuple[str, str, str]], list[PageResult]]:
    docs, errors = [], []
    for raw in paths:
        p = pathlib.Path(raw)
        files = (sorted(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in (".html", ".htm"))
                 if p.is_dir() else [p])
        if p.is_dir() and not files:
            errors.append(PageResult(str(p), 0, None, "directory holds no .html/.htm files"))
        for f in files:
            try:
                docs.append((str(f), f.read_text(encoding="utf-8", errors="replace"), _fmt_for(str(f), forced)))
            except OSError as e:
                errors.append(PageResult(str(f), 0, None, f"{type(e).__name__}: {e}"))
    return docs, errors


def _print_report(rep: ScanReport, *, as_json: bool, quiet: bool) -> None:
    for page in rep.pages:
        if as_json:
            print(json.dumps(page.to_dict(), ensure_ascii=False))
            continue
        if page.error:
            print(f"{page.url}: ERROR {page.error}")
            continue
        for v in page.audit.violations if page.audit else []:
            what = f"missing {v.label}; triggered by" if v.kind == "notice" else f"{v.label}:"
            print(f"{page.url}: [{v.action}] {v.rule_id} — {what} {v.excerpt[:200]!r}")
        if page.audit and not quiet:
            for note in page.audit.notes:
                print(f"{page.url}: note: {note}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="HTML / markdown files or directories of *.html")
    ap.add_argument("--site", default="", help="site hint (matches config site keys by substring)")
    ap.add_argument("--rule-set", action="append", default=[], help="rule set to apply (repeatable); "
                                                                     "overrides the site's opt-in list")
    ap.add_argument("--config", help="extra JSON config laid over the repo default and storage copy")
    ap.add_argument("--no-storage", action="store_true", help="ignore the storage copy of the config")
    ap.add_argument("--stdin", action="store_true", help="read one document from stdin")
    ap.add_argument("--format", choices=("auto", "html", "markdown"), default="auto",
                    help="document format (auto: .md/.markdown are markdown, everything else HTML; "
                         "stdin is HTML unless set)")
    ap.add_argument("--url", action="append", default=[], help="fetch and scan this URL (repeatable)")
    ap.add_argument("--base", help="site base URL for --sitemap / --crawl")
    ap.add_argument("--sitemap", action="store_true", help="scan the pages listed in <base>/sitemap.xml")
    ap.add_argument("--crawl", action="store_true", help="crawl from --base (or --url) on the same host")
    ap.add_argument("--path-re", default="", help="only scan pages whose path matches (e.g. '^/guides/')")
    ap.add_argument("--follow-re", default="", help="crawl: follow links whose path matches (default --path-re)")
    ap.add_argument("--never-fetch-re", default=DEFAULT_NEVER_FETCH,
                    help=f"paths never requested (default {DEFAULT_NEVER_FETCH!r}: click-outs log clicks)")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--max-pages", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between requests")
    ap.add_argument("--header", action="append", default=[], help="extra request header 'Name: value'")
    ap.add_argument("--strict", action="store_true", help="warn-level violations also exit 1")
    ap.add_argument("--allow-empty-policy", action="store_true",
                    help="exit 0 (instead of 2) when no rule set applies")
    ap.add_argument("--allow-errors", action="store_true",
                    help="pages that could not be read do not fail the run (default: exit 3)")
    ap.add_argument("--json", action="store_true", help="one JSON object per page")
    ap.add_argument("--report-json", help="write the full report here")
    ap.add_argument("--quiet", action="store_true", help="no notes, no summary line")
    args = ap.parse_args(argv)

    if not (args.paths or args.stdin or args.url or args.sitemap or args.crawl):
        ap.print_usage(sys.stderr)
        print("content_policy_scan: give files, --stdin, --url, or --base with --sitemap/--crawl",
              file=sys.stderr)
        return EXIT_USAGE
    if (args.sitemap or args.crawl) and not (args.base or args.url):
        print("content_policy_scan: --sitemap/--crawl need --base", file=sys.stderr)
        return EXIT_USAGE
    if args.sitemap and not args.base:
        # the site root of the first --url
        u = urllib.parse.urlsplit(args.url[0])
        if u.scheme not in ("http", "https") or not u.netloc:
            print(f"content_policy_scan: --sitemap needs --base (cannot derive it from {args.url[0]!r})",
                  file=sys.stderr)
            return EXIT_USAGE
        args.base = f"{u.scheme}://{u.netloc}"
    for flag, val in (("--base", args.base), *(("--url", x) for x in args.url)):
        if val and urllib.parse.urlsplit(val).scheme not in ("http", "https"):
            print(f"content_policy_scan: {flag} must be an http(s) URL, got {val!r}", file=sys.stderr)
            return EXIT_USAGE
    try:
        re.compile(args.never_fetch_re or "")
        re.compile(args.path_re or "")
        re.compile(args.follow_re or "")
    except re.error as e:
        print(f"content_policy_scan: bad regex: {e}", file=sys.stderr)
        return EXIT_USAGE
    try:
        cfg = load_config(None if args.no_storage else _storage(), path=args.config)
        policy = resolve_policy(args.site, config=cfg, rule_sets=args.rule_set or None)
    except ContentPolicyConfigError as e:
        print(f"content_policy_scan: config error: {e}", file=sys.stderr)
        return EXIT_USAGE
    if not policy.active:
        msg = (f"content_policy_scan: no rule set applies (site {args.site!r} opted into none; "
               f"pass --rule-set or opt the site in)")
        print(msg, file=sys.stderr)
        return EXIT_OK if args.allow_empty_policy else EXIT_USAGE

    headers = {}
    for h in args.header:
        if ":" not in h:
            print(f"content_policy_scan: bad --header {h!r} (want 'Name: value')", file=sys.stderr)
            return EXIT_USAGE
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()

    report = ScanReport(policy.rule_sets)
    docs, file_errors = _file_docs(args.paths, args.format)
    report.pages += file_errors
    for fmt in ("html", "markdown"):
        sub = [(name, text) for name, text, f in docs if f == fmt]
        if sub:
            report.pages += scan_documents(policy, sub, fmt=fmt).pages
    if args.stdin:
        fmt = "markdown" if args.format == "markdown" else "html"
        report.pages += scan_documents(policy, [("<stdin>", sys.stdin.read())], fmt=fmt).pages

    if args.url or args.sitemap or args.crawl:
        fetch = http_fetcher(timeout=args.timeout, headers=headers, pause_s=args.pause,
                             never_fetch_re=args.never_fetch_re)
        fetched = []
        if args.sitemap:
            try:
                urls = sitemap_urls(args.base, fetch, path_re=args.path_re, cap=args.max_pages,
                                    never_fetch_re=args.never_fetch_re)
            except (RuntimeError, ValueError) as e:
                report.pages.append(PageResult(args.base, 0, None, str(e)))
                urls = []
            fetched += [fetch(u) for u in urls]
        if args.crawl:
            fetched += crawl(args.url or [args.base], fetch, path_re=args.path_re, follow_re=args.follow_re,
                             never_fetch_re=args.never_fetch_re, depth=args.depth, cap=args.max_pages)
        elif args.url:
            fetched += [fetch(u) for u in args.url]
        report.pages += scan_fetched(policy, fetched).pages

    _print_report(report, as_json=args.json, quiet=args.quiet)
    if args.report_json:
        try:
            p = pathlib.Path(args.report_json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        except OSError as e:
            print(f"content_policy_scan: report not written: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"[content-policy] rule sets {', '.join(policy.rule_sets)}: {len(report.scanned)} page(s) "
              f"scanned, {report.rejects} reject, {report.warnings} warn, {len(report.errors)} error(s)",
              file=sys.stderr)
    if not report.scanned:
        return EXIT_NOTHING
    if report.rejects or (args.strict and report.warnings):
        return EXIT_VIOLATION
    if report.errors and not args.allow_errors:
        print(f"content_policy_scan: {len(report.errors)} page(s) could not be read; their content is "
              f"unverified (pass --allow-errors to accept that)", file=sys.stderr)
        return EXIT_NOTHING
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
