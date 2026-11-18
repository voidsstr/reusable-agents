import { BackgroundWriter, DEFAULT_MAX_PENDING_WRITES, defaultMaxSheddable } from '../../server/src/http/backgroundWriter';

function deferred(): { promise: Promise<void>; resolve: () => void; reject: (e: Error) => void } {
  let resolve!: () => void;
  let reject!: (e: Error) => void;
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('BackgroundWriter', () => {
  it('runs writes, counts them, and drain() waits for all of them', async () => {
    const lines: string[] = [];
    const w = new BackgroundWriter({ logError: (m) => lines.push(m) });
    const done: number[] = [];
    expect(w.submit('t', async () => { await new Promise((r) => setTimeout(r, 20)); done.push(1); })).toBe(true);
    expect(w.submit('t', async () => { done.push(2); })).toBe(true);
    expect(w.stats().pending).toBe(2);
    await w.drain();
    expect(done.sort()).toEqual([1, 2]);
    expect(w.stats()).toEqual({ submitted: 2, succeeded: 2, failed: 0, dropped: 0, shed: 0, pending: 0 });
    expect(lines).toEqual([]);
  });

  it('never lets a rejection or a synchronous throw escape', async () => {
    const lines: string[] = [];
    const w = new BackgroundWriter({ logError: (m) => lines.push(m) });
    w.submit('a', () => Promise.reject(new Error('db down')));
    w.submit('b', () => { throw new Error('bad params'); });
    await w.drain();
    expect(w.stats()).toMatchObject({ failed: 2, succeeded: 0 });
    expect(lines).toEqual([
      '[affiliate-kit] a write failed: db down',
      '[affiliate-kit] b write failed: bad params',
    ]);
  });

  it('logs the first failure per label, then one line per interval with the count', async () => {
    let now = 0;
    const lines: string[] = [];
    const w = new BackgroundWriter({ logError: (m) => lines.push(m), errorLogIntervalMs: 60_000, now: () => now });
    for (let i = 0; i < 5; i++) w.submit('outbound_clicks', () => Promise.reject(new Error('timeout')));
    await w.drain();
    expect(lines).toHaveLength(1);
    now = 61_000;
    w.submit('outbound_clicks', () => Promise.reject(new Error('timeout')));
    await w.drain();
    expect(lines).toEqual([
      '[affiliate-kit] outbound_clicks write failed: timeout',
      '[affiliate-kit] outbound_clicks write failed (5 failures): timeout',
    ]);
  });

  it('drops writes past the ceiling instead of queueing them', async () => {
    const lines: string[] = [];
    const w = new BackgroundWriter({ maxPending: 2, logError: (m) => lines.push(m) });
    const gate = deferred();
    expect(w.submit('ai', () => gate.promise)).toBe(true);
    expect(w.submit('ai', () => gate.promise)).toBe(true);
    let ran = false;
    expect(w.submit('ai', async () => { ran = true; })).toBe(false);
    expect(ran).toBe(false);
    expect(w.stats()).toMatchObject({ dropped: 1, pending: 2 });
    expect(lines).toEqual(['[affiliate-kit] ai: dropped 1 write(s), 2 already in flight']);
    gate.resolve();
    await w.drain();
    expect(w.submit('ai', async () => { ran = true; })).toBe(true);
    await w.drain();
    expect(ran).toBe(true);
  });

  it('sheds sheddable writes first, keeping the rest of the ceiling for ordinary ones', async () => {
    const lines: string[] = [];
    const w = new BackgroundWriter({ maxPending: 4, maxSheddable: 2, logError: (m) => lines.push(m) });
    const gate = deferred();
    expect(w.submit('ai_traffic_log', () => gate.promise, { sheddable: true })).toBe(true);
    expect(w.submit('ai_traffic_log', () => gate.promise, { sheddable: true })).toBe(true);
    expect(w.submit('ai_traffic_log', () => gate.promise, { sheddable: true })).toBe(false);
    // ordinary writes still have the reserved slots
    expect(w.submit('outbound_clicks', () => gate.promise)).toBe(true);
    expect(w.submit('outbound_clicks', () => gate.promise)).toBe(true);
    expect(w.submit('outbound_clicks', () => gate.promise)).toBe(false);
    // and a sheddable write counts every write in flight, not just its own kind
    expect(w.submit('ai_traffic_log', async () => undefined, { sheddable: true })).toBe(false);
    expect(w.stats()).toMatchObject({ submitted: 4, dropped: 3, shed: 2, pending: 4 });
    expect(lines).toEqual([
      '[affiliate-kit] ai_traffic_log: dropped 1 write(s), 2 already in flight',
      '[affiliate-kit] outbound_clicks: dropped 1 write(s), 4 already in flight',
    ]);
    gate.resolve();
    await w.drain();
    expect(w.stats()).toMatchObject({ succeeded: 4, pending: 0 });
  });

  it('defaults the sheddable ceiling to 3/4 of the total, at least 1', () => {
    expect(new BackgroundWriter().maxSheddable).toBe(48);
    expect(defaultMaxSheddable(64)).toBe(48);
    expect(defaultMaxSheddable(8)).toBe(6);
    expect(defaultMaxSheddable(2)).toBe(1);
    expect(defaultMaxSheddable(1)).toBe(1);
    expect(new BackgroundWriter({ maxPending: 10, maxSheddable: 10 }).maxSheddable).toBe(10);
  });

  it('survives a logger that throws', async () => {
    const w = new BackgroundWriter({ logError: () => { throw new Error('log sink down'); } });
    w.submit('x', () => Promise.reject(new Error('fail')));
    await expect(w.drain()).resolves.toBeUndefined();
  });

  it('validates the ceiling', () => {
    expect(DEFAULT_MAX_PENDING_WRITES).toBe(64);
    expect(() => new BackgroundWriter({ maxPending: 0 })).toThrow(RangeError);
    expect(() => new BackgroundWriter({ maxPending: 1.5 })).toThrow(RangeError);
    expect(() => new BackgroundWriter({ maxPending: 4, maxSheddable: 5 })).toThrow(RangeError);
    expect(() => new BackgroundWriter({ maxPending: 4, maxSheddable: 0 })).toThrow(RangeError);
    expect(() => new BackgroundWriter({ maxSheddable: 2.5 })).toThrow(RangeError);
  });
});
