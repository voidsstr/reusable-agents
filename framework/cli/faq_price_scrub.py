"""Strip stale prices (and any ratings in the same answers) from stored
product FAQ answers: local model first, Opus for whatever the guard rejects.

Why (2026-09-26): 1,247 active specpicks products carried FAQ answers that
quote a generation-day dollar price ("It has been listed around $18.99"),
which ships verbatim in the visible FAQ and in FAQPage JSON-LD. The
product-hydration prompt no longer writes prices, so this is a one-off
backlog cleanup, not an agent.

Per FAQ pair whose answer has a price (or, with --include-ratings-only, a
rating):
  1. The question is only about price or reviews ("How much does it cost?")
     -> drop the pair; no price-free answer to it is worth keeping.
  2. Rewrite with the fleet's resident local model (framework/core/local_llm:
     one model, one num_ctx, so this never makes Ollama swap models).
  3. framework.core.price_strip_guard checks the rewrite. Rejected ones are
     batched to Opus (claude-cli provider) and checked again.
  4. Still rejected -> drop the pair rather than keep a stale price.

Safety: dry-run by default (writes the proposed changes to a JSONL for
review). --apply backs up every product's original FAQ to
<backup-dir>/backup.jsonl BEFORE updating it and only updates a row whose FAQ
is still exactly what was read. A killed run resumes by re-running: processed
rows no longer match the price filter. Never deletes rows.

Choice of local model (2026-09-26 bake-off, ~/.reusable-agents/ktlo-work/
bakeoff): on FAQs qwen3.8:27b was usable on 58%, with 32% caught by the guard
and routed to Opus, leaving ~10% silent misses. gemma4:26b scored higher
alone but would force a model swap on the shared GPU.

Run it with the claude-pool shim first on PATH, as the fleet does, so Opus
calls use the pool and not a personal login:
  PATH=~/.reusable-agents/claude-pool/bin:$PATH \\
  python3 -m framework.cli.faq_price_scrub --dsn-env DATABASE_URL_SPECPICKS [--limit 20] [--apply]
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
    SYSTEM_PROMPT, check_rewrite, has_price, has_price_or_rating, is_price_or_review_question,
)

log = logging.getLogger("faq_price_scrub")
CALLER = "faq-price-scrub"


def _user_msg(q: str, a: str) -> str:
    return f"Question: {q}\n\nAnswer to rewrite:\n{a}"


def _needs_work(answer: str, include_ratings: bool) -> bool:
    return has_price_or_rating(answer) if include_ratings else has_price(answer)


def _local_rewrite(client, q: str, a: str) -> str:
    try:
        return (client.chat([{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": _user_msg(q, a)}],
                            temperature=0.2, max_tokens=900) or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("local rewrite failed: %s", e)
        return ""


def _opus_rewrite(client, batch: list[tuple[str, str]]) -> list[str]:
    """One Opus call for up to ~10 (question, answer) pairs; returns rewrites in order."""
    lines = ["Rewrite each numbered FAQ answer below following the instructions. "
             "Return ONLY a JSON array of strings, one rewritten answer per item, in the same order.\n"]
    for i, (q, a) in enumerate(batch):
        lines.append(f"#{i}\nQuestion: {q}\nAnswer to rewrite: {a}\n")
    try:
        raw = client.chat([{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": "\n".join(lines)}],
                          temperature=0.2, max_tokens=4000, timeout=600)
        arr = extract_json_array(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("opus batch failed: %s", e)
        return [""] * len(batch)
    out = [str(x).strip() if isinstance(x, str) else "" for x in arr]
    return (out + [""] * len(batch))[: len(batch)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn-env", required=True, help="env var holding the site's Postgres DSN")
    ap.add_argument("--table", default="products")
    ap.add_argument("--faq-column", default="faq")
    ap.add_argument("--limit", type=int, default=0, help="max products this run (0 = all)")
    ap.add_argument("--chunk", type=int, default=25, help="products per commit / Opus batching window")
    ap.add_argument("--opus-batch", type=int, default=10)
    ap.add_argument("--include-ratings-only", action="store_true",
                    help="also rewrite answers that carry ratings but no price")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    ap.add_argument("--backup-dir", default=os.path.expanduser("~/.reusable-agents/ktlo-work/faq-price-scrub"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import psycopg2
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"{args.dsn_env} is not set", file=sys.stderr)
        return 2
    bdir = Path(args.backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    backup_path, proposal_path = bdir / "backup.jsonl", bdir / "proposed.jsonl"
    # No "done" ledger: a processed row no longer matches the price filter
    # below (every priced answer is rewritten or dropped), so a killed run
    # resumes by simply running again. backup.jsonl only holds originals.

    local = ai_client_for(CALLER, override_provider="ollama-5090")
    opus = ai_client_for(CALLER, override_provider="claude-cli", override_model="claude-opus-5-5")

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    col, tbl = args.faq_column, args.table
    price_filter = r"\$[0-9]"
    cur.execute(
        f"SELECT id, asin, {col} FROM {tbl} WHERE is_active AND jsonb_typeof({col}) = 'array' "
        f"AND {col}::text ~ %s ORDER BY id", (price_filter,))
    rows = cur.fetchall()
    conn.rollback()
    if args.limit:
        rows = rows[: args.limit]
    stats = {"products": 0, "pairs_checked": 0, "dropped_price_question": 0, "local_ok": 0,
             "opus_ok": 0, "dropped_unfixable": 0, "updated": 0, "skipped_changed": 0}
    t0 = time.time()

    for c0 in range(0, len(rows), args.chunk):
        chunk = rows[c0: c0 + args.chunk]
        plans = []          # (id, asin, original, new_faq_slots)
        fallback = []       # (plan_index, slot_index, q, a)
        for (pid, asin, faq) in chunk:
            stats["products"] += 1
            slots = []
            for qa in faq:
                q, a = (qa or {}).get("question", ""), (qa or {}).get("answer", "")
                if not isinstance(a, str) or not _needs_work(a, args.include_ratings_only):
                    slots.append(("keep", qa))
                    continue
                stats["pairs_checked"] += 1
                if is_price_or_review_question(q):
                    stats["dropped_price_question"] += 1
                    slots.append(("drop", qa))
                    continue
                out = _local_rewrite(local, q, a)
                if check_rewrite(a, out).ok:
                    stats["local_ok"] += 1
                    slots.append(("rewrite", {**qa, "answer": out}))
                else:
                    slots.append(("pending", qa))
                    fallback.append((len(plans), len(slots) - 1, q, a))
            plans.append((pid, asin, faq, slots))

        for b0 in range(0, len(fallback), args.opus_batch):
            batch = fallback[b0: b0 + args.opus_batch]
            outs = _opus_rewrite(opus, [(q, a) for (_, _, q, a) in batch])
            for (pi, si, q, a), out in zip(batch, outs):
                qa = plans[pi][3][si][1]
                if check_rewrite(a, out).ok:
                    stats["opus_ok"] += 1
                    plans[pi][3][si] = ("rewrite", {**qa, "answer": out})
                else:
                    stats["dropped_unfixable"] += 1
                    plans[pi][3][si] = ("drop", qa)

        for (pid, asin, orig, slots) in plans:
            new_faq = [qa for (kind, qa) in slots if kind in ("keep", "rewrite")]
            if new_faq == orig:
                continue
            record = {"id": pid, "asin": asin, "original": orig, "new": new_faq,
                      "actions": [k for k, _ in slots]}
            if not args.apply:
                with proposal_path.open("a") as f:
                    f.write(json.dumps(record) + "\n")
                continue
            with backup_path.open("a") as f:
                f.write(json.dumps({"id": pid, "asin": asin, "original": orig}) + "\n")
            cur.execute(
                f"UPDATE {tbl} SET {col} = %s::jsonb WHERE id = %s AND {col} = %s::jsonb",
                (json.dumps(new_faq), pid, json.dumps(orig)))
            if cur.rowcount == 1:
                stats["updated"] += 1
            else:
                stats["skipped_changed"] += 1
        if args.apply:
            conn.commit()
        log.info("progress %d/%d products  %s  (%.0fs)", min(c0 + args.chunk, len(rows)), len(rows),
                 json.dumps(stats), time.time() - t0)

    stats["seconds"] = round(time.time() - t0)
    stats["mode"] = "apply" if args.apply else "dry-run"
    (bdir / f"stats-{stats['mode']}.json").write_text(json.dumps(stats, indent=1))
    print(json.dumps(stats))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
