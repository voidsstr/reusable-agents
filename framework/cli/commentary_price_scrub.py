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

Per batch of rows, one Opus call rewrites every priced field.
framework.core.price_strip_guard checks each rewrite with a number check, and
by default a second Opus call (judge_rewrites) reads every rewrite and lists
lost facts, unsupported or misleading claims and leftover prices. The number
check alone was not enough: a 300-row audit found dropped model names and
"a modest premium" written for a 47% price gap in rewrites it had passed. A
field the judge accepts is applied, even if the number check objected on
number or rating grounds. A rejected field gets one retry with the problems as
feedback. A field that still fails keeps its original text, is logged to
failed.jsonl with the rejected rewrite, and is skipped on later runs unless you
pass --retry-failed; --recheck-failed re-scores those saved rewrites without a
new rewrite call. --no-judge / --no-judge-all / --no-retry turn the stages off.

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
from framework.core.price_strip_guard import (
    SYSTEM_PROMPT, check_rewrite, has_price_or_rating, judge_rewrites, judgeable,
)

log = logging.getLogger("commentary_price_scrub")
FIELDS = ("verdict_reason", "buy_advice", "value_commentary")
# Other text of the same comparison. A number the model moves in from here is
# a fact from this comparison, not an invention.
CONTEXT_FIELDS = ("performance_commentary", "how_to_choose", "content_md")

BATCH_INSTRUCTIONS = (
    "Below are fields from product head-to-head comparisons on a PC-hardware site. "
    "Rewrite EACH field following the instructions: remove every price, price comparison, "
    "star rating and review count. Keep exactly: every benchmark and spec number, every product "
    "and model name (including the CPU or GPU inside each system), which product achieved which "
    "result, launch dates, and source attributions. Keep the verdict, winner and recommendation "
    "unchanged. Describe value in words that match the size of the gap in the source ('slightly "
    "cheaper', 'much more expensive', 'the cheaper card'); never soften or exaggerate it. Add no "
    "claim the source does not make (nothing about listing history, availability, sellers or age). "
    "Return ONLY a JSON array with one object per item, in order: "
    '{"id": <id>, "fields": {"<field name>": "<rewritten text>", ...}} containing exactly the '
    "fields given for that item.\n"
)
RETRY_INSTRUCTIONS = (
    "Your previous rewrites of the fields below had problems. Rewrite each SOURCE again under the "
    "same rules, fixing every listed problem and keeping everything else from the previous rewrite "
    "that was correct. Return ONLY a JSON array with one object per item, in order: "
    '{"id": <id>, "fields": {"<field name>": "<rewritten text>"}}.\n'
)


def _opus_batch(client, batch: list[dict]) -> dict[int, dict]:
    lines = [BATCH_INSTRUCTIONS]
    for it in batch:
        lines.append(f"### id {it['id']}  ({it['left']} vs {it['right']})")
        for f, v in it["fields"].items():
            lines.append(f"[{f}]\n{v}\n")
    return _opus_call(client, lines)


def _problems(res, verdict) -> list[str]:
    """Plain-language problems for a retry, from the guard result and the judge verdict."""
    out = []
    if "price_left" in res.reasons:
        out.append("a price or dollar amount is still in the text")
    if "scaffold_leak" in res.reasons or "empty" in res.reasons:
        out.append("the output was empty or contained labels instead of just the rewritten text")
    if verdict is not None:
        out += [f"restore this fact: {x}" for x in verdict.lost_facts]
        out += [f"remove this unsupported or misleading claim: {x}" for x in verdict.invented]
        if verdict.price_or_rating_left:
            out.append("a price amount, quantified price comparison, rating or review count is still in the text")
    elif "missing_numbers" in res.reasons:
        out.append(f"these numbers from the source are missing; restore any that are not price or rating data: "
                   f"{', '.join(res.missing_numbers)}")
    if "rating_left" in res.reasons and not (verdict and verdict.price_or_rating_left is False):
        out.append("a rating or review count may still be in the text")
    return out or ["the rewrite could not be verified; rewrite it again carefully"]


def _opus_retry(client, retries: list[dict]) -> dict[int, dict]:
    """retries: [{"id", "field", "source", "previous", "problems"}]."""
    lines = [RETRY_INSTRUCTIONS]
    by_id: dict = {}
    for r in retries:
        by_id.setdefault(r["id"], []).append(r)
    for rid, rs in by_id.items():
        lines.append(f"### id {rid}")
        for r in rs:
            lines.append(f"[{r['field']}]\nSOURCE:\n{r['source']}\n\nPREVIOUS REWRITE:\n{r['previous']}\n\n"
                         "PROBLEMS:\n" + "\n".join(f"- {p}" for p in r["problems"]) + "\n")
    return _opus_call(client, lines)


def _opus_call(client, lines: list[str]) -> dict[int, dict]:
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


def _evaluate(state: dict, keys: list, judge, judge_all: bool) -> None:
    """Set state[k]["res"], ["verdict"], ["ok"]. A field the judge reviewed is
    decided by the judge; otherwise by the number check alone."""
    pending = []
    for k in keys:
        st = state[k]
        st["res"] = check_rewrite(st["src"], st["out"], context=st["ctx"])
        if judge and ((st["res"].ok and judge_all) or judgeable(st["res"])):
            pending.append({"id": f"{k[0]}:{k[1]}", "source": st["src"], "rewrite": st["out"], "context": st["ctx"]})
    verdicts = _judge_pending(judge, pending)
    for k in keys:
        st = state[k]
        st["verdict"] = verdicts.get(f"{k[0]}:{k[1]}")
        st["ok"] = st["verdict"].ok if st["verdict"] is not None else st["res"].ok


def _judge_pending(judge, pending: list[dict]) -> dict:
    """pending: [{"id": key, "source", "rewrite", "context"}] -> {key: JudgeVerdict}."""
    if not judge or not pending:
        return {}
    return judge_rewrites(judge, pending)


def _recheck_failed(conn, bdir: Path, apply: bool, judge=None) -> int:
    """Apply saved rewrites that the current guard, or the judge, now accepts."""
    failed_path = bdir / "failed.jsonl"
    lines = failed_path.read_text().splitlines()
    entries = [json.loads(line) for line in lines if line.strip()]
    cur = conn.cursor()
    ids = sorted({e["id"] for e in entries})
    cur.execute(f"SELECT id, {', '.join(FIELDS + CONTEXT_FIELDS)} FROM comparison_commentary WHERE id = ANY(%s)", (ids,))
    rows = {r[0]: (dict(zip(FIELDS, r[1:1 + len(FIELDS)])), " ".join(str(v) for v in r[1 + len(FIELDS):] if v))
            for r in cur.fetchall()}
    keep, stats = [], {"entries": len(entries), "now_ok": 0, "judged_ok": 0, "applied": 0,
                       "changed_underneath": 0, "still_failed": 0}
    bk = open(bdir / "backup.jsonl", "a")
    accepted, pending = [], []
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
        e = {**e, "reasons": res.reasons, "missing": res.missing_numbers, "new": res.new_numbers}
        if res.ok:
            stats["now_ok"] += 1
            accepted.append((e, cur_fields))
        elif judge and judgeable(res):
            pending.append((e, cur_fields, ctx))
        else:
            stats["still_failed"] += 1
            keep.append(e)
    verdicts = _judge_pending(judge, [{"id": f"{e['id']}:{e['field']}", "source": e["src"], "rewrite": e["out"],
                                       "context": ctx} for e, _, ctx in pending])
    for e, cur_fields, _ in pending:
        v = verdicts.get(f"{e['id']}:{e['field']}")
        if v and v.ok:
            stats["judged_ok"] += 1
            accepted.append((e, cur_fields))
        else:
            stats["still_failed"] += 1
            keep.append({**e, "judge": v and {"lost": v.lost_facts, "invented": v.invented,
                                              "left": v.price_or_rating_left, "error": v.error}})
    for e, cur_fields in accepted:
        if not apply:
            keep.append(e)
            continue
        src, out = e["src"], e["out"]
        bk.write(json.dumps({"id": e["id"], "original": cur_fields}) + "\n")
        bk.flush()
        cur.execute(f"UPDATE comparison_commentary SET {e['field']} = %s, updated_at = now() "
                    f"WHERE id = %s AND {e['field']} = %s", (out, e["id"], src))
        stats["applied"] += cur.rowcount
    if apply:
        conn.commit()
        # A concurrent scrub run may have appended rejections while this ran; keep them.
        appended = failed_path.read_text().splitlines()[len(lines):]
        failed_path.write_text("".join(json.dumps(k) + "\n" for k in keep)
                               + "".join(line + "\n" for line in appended))
    print(json.dumps(stats))
    conn.close()
    return 0


def _audit_applied(conn, bdir: Path, opus, judge, args) -> int:
    """Second-pass audit of rewrites applied before the judge existed."""
    if judge is None:
        print("--audit-applied needs the judge", file=sys.stderr)
        return 2
    applied = [json.loads(x) for x in (bdir / "applied.jsonl").read_text().splitlines() if x.strip()]
    audit_path = bdir / ("audit.jsonl" if args.apply else "audit-dry.jsonl")
    done = set()
    if audit_path.exists():
        for x in audit_path.read_text().splitlines():
            d = json.loads(x)
            done.add((d["id"], d["field"]))
    todo = [(a["id"], f, a["old"][f], a["new"][f]) for a in applied for f in a["new"] if (a["id"], f) not in done]
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        todo = [t for t in todo if t[0] % n == i]
    if args.limit:
        todo = todo[: args.limit]
    cur = conn.cursor()
    ids = sorted({t[0] for t in todo})
    cur.execute(f"SELECT id, {', '.join(FIELDS + CONTEXT_FIELDS)} FROM comparison_commentary WHERE id = ANY(%s)", (ids,))
    live = {r[0]: (dict(zip(FIELDS, r[1:1 + len(FIELDS)])), " ".join(str(v) for v in r[1 + len(FIELDS):] if v))
            for r in cur.fetchall()}
    conn.rollback()
    stats = {"fields": len(todo), "ok": 0, "fixed": 0, "restored": 0, "changed_since": 0}
    log.info("audit: %s", json.dumps(stats))
    out = open(audit_path, "a")
    bk = open(bdir / "backup.jsonl", "a")
    t0 = time.time()
    for b0 in range(0, len(todo), 12):
        chunk = []
        for rid, f, old, new in todo[b0: b0 + 12]:
            fields, context = live.get(rid, ({}, ""))
            if fields.get(f) != new:  # edited since the scrub applied it
                stats["changed_since"] += 1
                out.write(json.dumps({"id": rid, "field": f, "action": "changed_since"}) + "\n")
                continue
            ctx = " ".join([context, *(str(v) for k, v in fields.items() if k != f and v)])
            chunk.append({"key": (rid, f), "src": old, "out": new, "ctx": ctx, "live": new})
        state = {c["key"]: c for c in chunk}
        _evaluate(state, list(state), judge, True)
        # A judge error means "not verified", not "bad": leave the field alone and
        # let the next run retry it (never restore wholesale during a pool outage).
        for k in [k for k, st in state.items() if st["verdict"] is not None and st["verdict"].error]:
            state.pop(k)
            stats["skipped"] = stats.get("skipped", 0) + 1
        bad = [k for k, st in state.items() if not st["ok"]]
        first = {k: (state[k]["out"], state[k]["verdict"]) for k in bad}
        if bad:
            again = _opus_retry(opus, [{"id": k[0], "field": k[1], "source": state[k]["src"], "previous": state[k]["out"],
                                        "problems": _problems(state[k]["res"], state[k]["verdict"])} for k in bad])
            for k in bad:
                state[k]["out"] = (again.get(k[0]) or {}).get(k[1], "") or state[k]["out"]
            _evaluate(state, bad, judge, True)
        for k, st in list(state.items()):
            if k in first and st["verdict"] is not None and st["verdict"].error:
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            rid, f = k
            if k not in first:
                action, text = "ok", None
            elif st["ok"]:
                action, text = "fixed", st["out"]
            else:
                action, text = "restored", st["src"]
            stats[action] += 1
            v = first.get(k, (None, None))[1]
            out.write(json.dumps({"id": rid, "field": f, "action": action,
                                  "flag": v and {"lost": v.lost_facts, "invented": v.invented, "left": v.price_or_rating_left,
                                                 "error": v.error},
                                  "text": text}) + "\n")
            if text is None or not args.apply:
                continue
            bk.write(json.dumps({"id": rid, "field": f, "audited_live": st["live"]}) + "\n")
            bk.flush()
            cur.execute(f"UPDATE comparison_commentary SET {f} = %s, updated_at = now() WHERE id = %s AND {f} = %s",
                        (text, rid, st["live"]))
        if args.apply:
            conn.commit()
        out.flush()
        log.info("audit %d/%d %s (%.0fs)", min(b0 + 12, len(todo)), len(todo), json.dumps(stats), time.time() - t0)
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
    ap.add_argument("--no-judge", action="store_true",
                    help="do not use the Opus judge (number check only)")
    ap.add_argument("--judge-all", action=argparse.BooleanOptionalAction, default=True,
                    help="judge every rewrite, not only number/rating rejections (default on: the number "
                         "check alone let dropped model names and misleading value wording through)")
    ap.add_argument("--no-retry", action="store_true",
                    help="do not give rejected fields one retry with the problems as feedback")
    ap.add_argument("--shard", default="", help="i/n: only rows with id %% n == i (run n workers in parallel)")
    ap.add_argument("--audit-applied", action="store_true",
                    help="judge rewrites an earlier run applied (applied.jsonl) that are still live; retry "
                         "flagged ones with feedback and restore the original when the retry also fails")
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

    opus = ai_client_for("commentary-price-scrub", override_provider="claude-cli",
                         override_model="claude-opus-5-5")
    judge = None if args.no_judge else opus
    if args.audit_applied:
        return _audit_applied(psycopg2.connect(dsn), bdir, opus, judge, args)
    if args.recheck_failed:
        return _recheck_failed(psycopg2.connect(dsn), bdir, args.apply, judge)

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
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        work = [w for w in work if w["id"] % n == i]
    if args.limit:
        work = work[: args.limit]
    stats = {"rows": len(work), "fields": sum(len(w["fields"]) for w in work),
             "fields_ok": 0, "fields_retry_ok": 0, "fields_failed": 0, "rows_updated": 0,
             "rows_changed_underneath": 0}
    log.info("to process: %s", json.dumps(stats))
    t0 = time.time()
    prop = open(bdir / ("applied.jsonl" if args.apply else "proposed.jsonl"), "a")
    bk = open(bdir / "backup.jsonl", "a")
    fl = open(failed_path, "a")

    for b0 in range(0, len(work), args.batch):
        batch = work[b0: b0 + args.batch]
        got = _opus_batch(opus, batch)
        if not got:  # the rewrite call failed (pool outage, timeout): leave the rows for the next run
            stats["batches_skipped"] = stats.get("batches_skipped", 0) + 1
            continue
        state = {}
        for it in batch:
            for f, src in it["fields"].items():
                state[(it["id"], f)] = {
                    "src": src, "out": (got.get(it["id"]) or {}).get(f, ""),
                    "ctx": " ".join([it["context"], *(str(v) for k, v in it["orig"].items() if k != f and v)])}
        keys = list(state)
        _evaluate(state, keys, judge, args.judge_all)
        bad = [k for k in keys if not state[k]["ok"]]
        if bad and not args.no_retry:
            again = _opus_retry(opus, [{"id": k[0], "field": k[1], "source": state[k]["src"],
                                        "previous": state[k]["out"],
                                        "problems": _problems(state[k]["res"], state[k]["verdict"])} for k in bad])
            for k in bad:
                state[k]["first_try"] = state[k]["out"]
                state[k]["out"] = (again.get(k[0]) or {}).get(k[1], "") or state[k]["out"]
            _evaluate(state, bad, judge, args.judge_all)
        for it in batch:
            new_fields = {}
            for f in it["fields"]:
                st = state[(it["id"], f)]
                if st["ok"]:
                    new_fields[f] = st["out"]
                    stats["fields_retry_ok" if "first_try" in st else "fields_ok"] += 1
                else:
                    stats["fields_failed"] += 1
                    res, v = st["res"], st["verdict"]
                    fl.write(json.dumps({"id": it["id"], "field": f, "reasons": res.reasons,
                                         "missing": res.missing_numbers, "new": res.new_numbers,
                                         "judge": v and {"lost": v.lost_facts, "invented": v.invented,
                                                         "left": v.price_or_rating_left, "error": v.error},
                                         "src": st["src"], "out": st["out"]}) + "\n")
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
