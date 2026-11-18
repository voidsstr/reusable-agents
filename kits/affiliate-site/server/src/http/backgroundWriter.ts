/**
 * Fire-and-forget database writes with a ceiling.
 *
 * SpecPicks writes its click and AI-traffic rows with `pool.query(...).catch()`
 * after the response is sent: a log write must never delay or fail a page.
 * The kit keeps that, and adds two things the shared database needs:
 *
 *   - a ceiling on writes in flight. Each pending write holds (or waits for)
 *     a pool connection; a crawler storm or a beacon flood during a slow
 *     database would otherwise queue thousands of them in front of page
 *     renders. Past `maxPending` the row is dropped and counted.
 *   - a lower ceiling for sheddable writes (the AI-traffic log). They are
 *     dropped once `maxSheddable` writes of any kind are in flight, so the
 *     rest of the ceiling stays free for the rows that matter most (click
 *     rows: SpecPicks never dropped one). A crawler burst during a slow
 *     database therefore sheds AI-log rows first, never the verified-human
 *     beacon behind it.
 *   - rate-limited error lines. A database outage fails every write; one
 *     line per label per interval (with the count since the last line) says
 *     so without flooding the container log.
 *
 * drain() resolves once every accepted write has settled: tests await it,
 * and a graceful shutdown can too.
 */

export interface BackgroundWriterOptions {
  /** Writes allowed in flight before new ones are dropped (default 64). */
  maxPending?: number;
  /**
   * Writes in flight (of any kind) past which a SHEDDABLE write is dropped;
   * 1..maxPending, default 3/4 of maxPending (at least 1). The difference is
   * reserved for ordinary writes.
   */
  maxSheddable?: number;
  /** Where failures and drops are reported (default console.error). */
  logError?: (msg: string) => void;
  /** At most one failure line and one drop line per label per interval (default 60 s). */
  errorLogIntervalMs?: number;
  now?: () => number;
}

export interface BackgroundWriteStats {
  readonly submitted: number;
  readonly succeeded: number;
  readonly failed: number;
  /** Every dropped write, sheddable ones included. */
  readonly dropped: number;
  /** Of `dropped`, the sheddable writes. */
  readonly shed: number;
  readonly pending: number;
}

export interface SubmitOptions {
  /** Drop this write first under load (at maxSheddable in flight instead of maxPending). */
  sheddable?: boolean;
}

export const DEFAULT_MAX_PENDING_WRITES = 64;
export const DEFAULT_WRITE_ERROR_LOG_INTERVAL_MS = 60_000;

/** The default sheddable ceiling: 3/4 of maxPending, at least 1 (48 of 64). */
export function defaultMaxSheddable(maxPending: number): number {
  return Math.max(1, Math.floor((maxPending * 3) / 4));
}

interface Throttle {
  lastAt: number;
  suppressed: number;
}

export class BackgroundWriter {
  readonly maxPending: number;
  readonly maxSheddable: number;
  private readonly logError: (msg: string) => void;
  private readonly intervalMs: number;
  private readonly now: () => number;
  private readonly inflight = new Set<Promise<void>>();
  private readonly throttles = new Map<string, Throttle>();
  private counts = { submitted: 0, succeeded: 0, failed: 0, dropped: 0, shed: 0 };

  constructor(opts: BackgroundWriterOptions = {}) {
    const max = opts.maxPending ?? DEFAULT_MAX_PENDING_WRITES;
    if (!Number.isInteger(max) || max < 1) throw new RangeError(`maxPending must be a positive integer (got ${max})`);
    const shed = opts.maxSheddable ?? defaultMaxSheddable(max);
    if (!Number.isInteger(shed) || shed < 1 || shed > max) {
      throw new RangeError(`maxSheddable must be an integer from 1 to maxPending (${max}) (got ${shed})`);
    }
    this.maxPending = max;
    this.maxSheddable = shed;
    // eslint-disable-next-line no-console
    this.logError = opts.logError ?? ((m) => console.error(m));
    this.intervalMs = opts.errorLogIntervalMs ?? DEFAULT_WRITE_ERROR_LOG_INTERVAL_MS;
    this.now = opts.now ?? Date.now;
  }

  /**
   * Start `task` now unless its ceiling is reached (maxPending, or
   * maxSheddable for a sheddable write). Returns false when the write was
   * dropped. Never throws, and the task's rejection never escapes.
   */
  submit(label: string, task: () => Promise<unknown>, opts: SubmitOptions = {}): boolean {
    const cap = opts.sheddable ? this.maxSheddable : this.maxPending;
    if (this.inflight.size >= cap) {
      this.counts.dropped++;
      if (opts.sheddable) this.counts.shed++;
      this.report(`${label}:drop`, (n) => `[affiliate-kit] ${label}: dropped ${n} write(s), ${cap} already in flight`);
      return false;
    }
    this.counts.submitted++;
    let started: Promise<unknown>;
    try {
      started = Promise.resolve(task());
    } catch (e) {
      started = Promise.reject(e);
    }
    const tracked: Promise<void> = started.then(
      () => { this.counts.succeeded++; },
      (e: unknown) => {
        this.counts.failed++;
        const why = e instanceof Error ? e.message : String(e);
        this.report(`${label}:fail`, (n) => `[affiliate-kit] ${label} write failed${n > 1 ? ` (${n} failures)` : ''}: ${why}`);
      },
    ).finally(() => { this.inflight.delete(tracked); });
    this.inflight.add(tracked);
    return true;
  }

  /** Resolves when every write accepted so far (and any started meanwhile) has settled. */
  async drain(): Promise<void> {
    while (this.inflight.size) await Promise.allSettled(Array.from(this.inflight));
  }

  stats(): BackgroundWriteStats {
    return { ...this.counts, pending: this.inflight.size };
  }

  /** Log the first event per key, then at most one line per interval carrying the count since. */
  private report(key: string, line: (count: number) => string): void {
    const now = this.now();
    const t = this.throttles.get(key);
    if (!t) {
      this.throttles.set(key, { lastAt: now, suppressed: 0 });
      this.emit(line(1));
      return;
    }
    t.suppressed++;
    if (now - t.lastAt >= this.intervalMs) {
      const n = t.suppressed;
      t.lastAt = now;
      t.suppressed = 0;
      this.emit(line(n));
    }
  }

  private emit(msg: string): void {
    try {
      this.logError(msg);
    } catch {
      /* logging must never break a request */
    }
  }
}
