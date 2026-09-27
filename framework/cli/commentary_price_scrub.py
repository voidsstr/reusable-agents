"""Rewrite stored head-to-head verdict fields price-free and rating-free, with Opus.

Why (2026-09-26): 7,300 of 7,432 specpicks comparison_commentary rows quote
generation-day prices or scraped ratings in verdict_reason / buy_advice /
value_commentary. They go stale within days, and the 24h price rule keeps a
priced verdict out of the server-rendered /vs/ page (only ~625 qualified).
The h2h prompt no longer asks for prices (specpicks d404f4b); this clears the
backlog.

Head-to-head verdicts are Opus-only editorial prose (CLAUDE.md), so there is
no local-model path here. Only the priced FIELDS are rewritten, not whole
comparisons; content_md is sanitized at render time
(specpicks vsPair.sanitizeCommentary).

Per batch of rows, one Opus call rewrites every priced field;
framework.core.price_strip_guard checks each one. A field that fails keeps its
original text, is logged to failed.jsonl, and is skipped on later runs unless
you pass --retry-failed.

Safety: dry-run by default. --apply appends originals to
<backup-dir>/backup.jsonl before the UPDATE, and only updates a row whose
fields still match what was read. Resumable: rewritten rows no longer match.

  PATH=~/.reusable-agents/claude-pool/bin:$PATH \\
  python3 -m framework.cli.commentary_price_scrub --dsn-env DATABASE_URL_SPECPICKS [--limit N] [--apply]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from framework.core.ai_providers import ai_client_for
from framework.core.llm_json import extract_json_array
from framework.core.price_strip_guard import SYSTEM_PROMPT, check_rewrite, has_price_or_rating

log = logging.getLogger("commentary_price_scrub")
FIELDS = ("verdict_reason", "buy_advice", "value_commentary")
# Other text of the same comparison. A number the model moves in from here is
# a fact from this comparison, not an invention.
CONTEXT_FIELDS = ("performance_commentary", "how_to_choose", "content_md")

BATCH_INSTRUCTIONS = (
    "Below are fields from product head-to-head comparisons on a PC-hardware site. "
    "Rewrite EACH field following the instructions: remove every price, price comparison, "
    "star rating and review count; keep every benchmark and spec number exactly; keep the "
    "verdict, winner and recommendation unchanged; express value in words ('the cheaper card', "
    "'a modest premium'). Return ONLY a JSON array with one object per item, in order: "
    '{"id": <id>, "fields": {"<field name>": "<rewritten text>", ...}} containing exactly the '
    "fields given for that item.\n"
)


def _opus_batch(client, batch: list[dict]) -> dict[int, dict]:
    lines = [BATCH_INSTRUCTIONS]
    for it in batch:
        lines.append(f"### id {it['id']}  ({it['left']} vs {it['right']})")
        for f, v in it["fields"].items():
            lines.append(f"[{f}]\n{v}\n")
    try:
        raw = client.chat([{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": "\n".join(lines)}],
                          temperature=0.2, max_tokens=16000, timeout=900)
        arr = extract_json_array(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("opus batch failed: %s", e)
        return {}
    out = {}
    for obj in arr:
        if isinstance(obj, dict) and isinstance(obj.get("fields"), dict):
            try:
                out[int(obj["id"])] = obj["fields"]
            except (TypeError, ValueError, KeyError):
                pass
    return out


def _recheck_failed(conn, bdir: Path, apply: bool) -> int:
    """Apply saved rewrites that the (since improved) guard now accepts."""
    failed_path = bdir / "failed.jsonl"
    entries = [json.loads(line) for line in failed_path.read_text().splitlines() if line.strip()]
    cur = conn.cursor()
    ids = sorted({e["id"] for e in entries})
    cur.execute(f"SELECT id, {', '.join(FIELDS + CONTEXT_FIELDS)} FROM comparison_commentary WHERE id = ANY(%s)", (ids,))
    rows = {r[0]: (dict(zip(FIELDS, r[1:1 + len(FIELDS)])), " ".join(str(v) for v in r[1 + len(FIELDS):] if v))
            for r in cur.fetchall()}
    keep, stats = [], {"entries": len(entries), "now_ok": 0, "applied": 0, "changed_underneath": 0, "still_failed": 0}
    bk = open(bdir / "backup.jsonl", "a")
    for e in entries:
        cur_fields, context = rows.get(e["id"], ({}, ""))
        src, out = e.get("src"), e.get("out")
        # Only when the live field still holds the source the rewrite was made from.
        if not src or not out or cur_fields.get(e["field"]) != src:
            stats["changed_underneath" if src and out else "still_failed"] += 1
            if not (src and out):
                keep.append(e)
            continue
        ctx = " ".join([context, *(str(v) for k, v in cur_fields.items() if k != e["field"] and v)])
        res = check_rewrite(src, out, context=ctx)
        if not res.ok:
            stats["still_failed"] += 1
            keep.append({**e, "reasons": res.reasons, "missing": res.missing_numbers, "new": res.new_numbers})
            continue
        stats["now_ok"] += 1
        if not apply:
            keep.append(e)
            continue
        bk.write(json.dumps({"id": e["id"], "original": cur_fields}) + "\n")
        bk.flush()
        cur.execute(f"UPDATE comparison_commentary SET {e['field']} = %s, updated_at = now() "
                    f"WHERE id = %s AND {e['field']} = %s", (out, e["id"], src))
        stats["applied"] += cur.rowcount
    if apply:
        conn.commit()
        failed_path.write_text("".join(json.dumps(k) + "\n" for k in keep))
    print(json.dumps(stats))
    conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn-env", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=6, help="comparisons per Opus call")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--recheck-failed", action="store_true",
                    help="re-score the rewrites saved in failed.jsonl with the current guard and apply "
                         "those that now pass (no model calls); the rest stay in failed.jsonl")
    ap.add_argument("--backup-dir", default=os.path.expanduser("~/.reusable-agents/ktlo-work/commentary-scrub"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import psycopg2
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"{args.dsn_env} is not set", file=sys.stderr)
        return 2
    bdir = Path(args.backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    failed_path = bdir / "failed.jsonl"
    failed = set()
    if failed_path.exists() and not args.retry_failed:
        for line in failed_path.read_text().splitlines():
            try:
                d = json.loads(line)
                failed.add((d["id"], d["field"]))
            except (ValueError, KeyError):
                pass

    if args.recheck_failed:
        return _recheck_failed(psycopg2.connect(dsn), bdir, args.apply)

    opus = ai_client_for("commentary-price-scrub", override_provider="claude-cli",
                         override_model="claude-opus-5-5")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(f"SELECT id, left_ref, right_ref, {', '.join(FIELDS + CONTEXT_FIELDS)} "
                "FROM comparison_commentary ORDER BY updated_at DESC")
    work = []
    for row in cur.fetchall():
        rid, l, r, *vals = row
        ctx_vals = vals[len(FIELDS):]
        vals = vals[: len(FIELDS)]
        fields = {f: v for f, v in zip(FIELDS, vals)
                  if v and has_price_or_rating(v) and (rid, f) not in failed}
        if fields:
            work.append({"id": rid, "left": l, "right": r, "fields": fields,
                         "orig": dict(zip(FIELDS, vals)),
                         "context": " ".join(str(v) for v in ctx_vals if v)})
    conn.rollback()
    if args.limit:
        work = work[: args.limit]
    stats = {"rows": len(work), "fields": sum(len(w["fields"]) for w in work),
             "fields_ok": 0, "fields_failed": 0, "rows_updated": 0, "rows_changed_underneath": 0}
    log.info("to process: %s", json.dumps(stats))
    t0 = time.time()
    prop = open(bdir / ("applied.jsonl" if args.apply else "proposed.jsonl"), "a")
    bk = open(bdir / "backup.jsonl", "a")
    fl = open(failed_path, "a")

    for b0 in range(0, len(work), args.batch):
        batch = work[b0: b0 + args.batch]
        got = _opus_batch(opus, batch)
        for it in batch:
            new_fields = {}
            for f, src in it["fields"].items():
                out = (got.get(it["id"]) or {}).get(f, "")
                ctx = " ".join([it["context"], *(str(v) for k, v in it["orig"].items() if k != f and v)])
                res = check_rewrite(src, out, context=ctx)
                if res.ok:
                    new_fields[f] = out
                    stats["fields_ok"] += 1
                else:
                    stats["fields_failed"] += 1
                    fl.write(json.dumps({"id": it["id"], "field": f, "reasons": res.reasons,
                                         "missing": res.missing_numbers, "new": res.new_numbers,
                                         "src": src, "out": out}) + "\n")
            if not new_fields:
                continue
            prop.write(json.dumps({"id": it["id"], "old": {f: it["orig"][f] for f in new_fields},
                                   "new": new_fields}) + "\n")
            if not args.apply:
                continue
            bk.write(json.dumps({"id": it["id"], "original": it["orig"]}) + "\n")
            bk.flush()
            sets = ", ".join(f"{f} = %s" for f in new_fields)
            where = " AND ".join(f"{f} IS NOT DISTINCT FROM %s" for f in FIELDS)
            cur.execute(f"UPDATE comparison_commentary SET {sets}, updated_at = now() "
                        f"WHERE id = %s AND {where}",
                        [*new_fields.values(), it["id"], *[it["orig"][f] for f in FIELDS]])
            if cur.rowcount == 1:
                stats["rows_updated"] += 1
            else:
                stats["rows_changed_underneath"] += 1
        if args.apply:
            conn.commit()
        fl.flush(); prop.flush()
        log.info("progress %d/%d rows %s (%.0fs)", min(b0 + args.batch, len(work)), len(work),
                 json.dumps(stats), time.time() - t0)

    stats["seconds"] = round(time.time() - t0)
    print(json.dumps(stats))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
