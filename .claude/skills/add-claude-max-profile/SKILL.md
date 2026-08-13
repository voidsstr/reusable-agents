---
name: add-claude-max-profile
description: |
  Add or replace a Claude Max account in the reusable-agents claude-pool
  so authoring agents (article-author, news-writer, h2h) have more Opus
  weekly capacity. Invoke when the operator says "add a max account",
  "add another claude account", "the pool is exhausted, let's add
  capacity", "log in another profile", "I have a new max account",
  "remove the duplicate profile and add a new one", or asks how to
  expand pool throughput. The skill wraps install/add-claude-profile.sh
  which supports two modes — allocate the next profile-N slot, OR wipe
  an existing slot (--reuse-slot N) when a duplicate account is
  occupying it. After login the script registers the profile in
  ~/.reusable-agents/claude-pool/state.json so the rotation picks it up.
---

# Add a Claude Max profile to the pool

The `claude-pool` (`~/.reusable-agents/claude-pool/`) rotates `claude`
subprocess calls across N profile dirs, each backed by a separate Max
account. More distinct accounts = more Opus weekly capacity for article
authoring without any quality compromise (the alternative — falling back
to Sonnet/Haiku for article bodies — is forbidden by the quality
contract).

This skill walks the operator through adding one new profile.

## When to invoke

- Operator asks to add a Max account, expand the pool, or log in another
  profile.
- Pipeline-monitor reports all profiles exhausted with reset times >24h
  out (weekly cap), publish volume is dropping, and the operator wants
  more capacity rather than waiting.
- After a new Max subscription is purchased, before it's registered in
  the pool.

## Before you start — sanity checks

1. **Is the operator at an interactive terminal?** Login requires the
   operator to paste a URL into their browser and authenticate. If you
   were spawned by a cron / background loop, surface "operator needs to
   run this interactively" rather than blocking.
2. **Read current pool state** so you can report which slot is next and
   what accounts are already in:
   ```bash
   python3 -c "
   import json
   s = json.load(open('/home/voidsstr/.reusable-agents/claude-pool/state.json'))
   for pid, info in sorted(s.items()):
       if pid.startswith('__'): continue
       label = info.get('label','')
       auth = 'yes' if info.get('authenticated') else 'NO'
       print(f'{pid}  auth={auth}  label={label}')
   "
   ```
3. **Detect duplicate accounts and offer to replace one.** Two profiles
   backed by the same email = same weekly quota = no actual capacity
   gain. Walk every `~/.reusable-agents/claude-pool/profile-N/.claude.json`,
   group by `oauthAccount.emailAddress` (or `accountUuid`), and report
   collisions. If duplicates exist, recommend the operator use
   `--reuse-slot N` against the HIGHER-numbered duplicate (lower number
   has more history; preserves round-robin fairness) rather than
   allocating a new slot.

   ```bash
   for n in $(seq 1 10); do
     cj=~/.reusable-agents/claude-pool/profile-$n/.claude.json
     [[ -f "$cj" ]] || continue
     email=$(python3 -c "import json; print(json.load(open('$cj')).get('oauthAccount',{}).get('emailAddress',''))")
     echo "profile-$n  $email"
   done | sort -k2 | uniq -f1 -D   # prints only the duplicates
   ```

## How to run

The script lives at
`/home/voidsstr/development/reusable-agents/install/add-claude-profile.sh`:

```bash
bash /home/voidsstr/development/reusable-agents/install/add-claude-profile.sh
```

It will:
1. Auto-detect the next `profile-N` slot (scans the pool dir).
2. Create the home dir with symlinks for `.bashrc`, `.gitconfig`,
   `.npmrc`.
3. Run `HOME=<profile_dir> claude /login` interactively — the operator
   authenticates in a browser, then control returns to the script.
4. Read the resulting `.claude.json` to confirm the email address.
5. Register the new profile in `state.json` with `authenticated=true`,
   `total_uses=0`, `discovered_at=<now>`, and the email as `label`.
6. Print a table of all profiles + auth status.

Optional proxy flag — only use if the operator explicitly wants this
profile to route through a different IP (e.g. to spread accounts across
networks per Anthropic ToS):

```bash
bash install/add-claude-profile.sh --proxy socks5://127.0.0.1:1080
```

The proxy is recorded in `~/.reusable-agents/claude-pool/proxies.conf`
as `profile-N=<url>` and consumed by `framework/cli/claude_pool.py` when
that profile is dispatched.

## Because YOU cannot run interactive `claude /login`

You (the assistant) cannot complete the browser auth — only the operator
can. The correct interaction:

1. Tell the operator: "Run this in your terminal:
   `bash /home/voidsstr/development/reusable-agents/install/add-claude-profile.sh`"
2. Wait for them to report "done" or "logged in".
3. Verify the new profile registered correctly:
   ```bash
   python3 -c "
   import json
   s = json.load(open('/home/voidsstr/.reusable-agents/claude-pool/state.json'))
   pids = sorted([k for k in s if k.startswith('profile-')], key=lambda x: int(x.split('-')[1]))
   last = pids[-1]
   print(json.dumps({last: s[last]}, indent=2))
   "
   ```
4. Confirm the email shown matches what the operator just logged in
   with, and that `authenticated: true`.

If `authenticated` is `false` or `.claude.json` is missing, the login
didn't complete — ask the operator to re-run. Don't try to fake the
state.json entry yourself; the pool's first dispatch will discover the
auth is broken and mark it dead again.

## Verifying the pool now picks up the new profile

After registration:

1. **Smoke-test the profile** by running `claude` once with its HOME:
   ```bash
   HOME=/home/voidsstr/.reusable-agents/claude-pool/profile-N \
     claude --print --model claude-opus-5 "ping" 2>&1 | head -5
   ```
   Expect a one-line response, not a 401 or "weekly limit" message.
2. **Watch the next authoring run** pick up the new profile — the pool
   rotates round-robin biased by `total_uses` (lowest first), so the
   fresh profile will be selected on the next dispatch:
   ```bash
   tail -f ~/.reusable-agents/data/agents/*-article-author-agent/runs/*/decisions.jsonl \
     2>/dev/null | grep -E "claude-pool|profile-"
   ```
3. **Confirm publish volume recovers** within an hour or two — query the
   site DB:
   ```bash
   . ~/.reusable-agents/secrets.env
   psql "$DATABASE_URL_AISLEPROMPT" -c \
     "SELECT COUNT(*) FROM editorial_articles WHERE created_at > now() - interval '2 hours';"
   ```

## Anti-patterns (refuse on sight)

- **"Just edit state.json to add the entry"** — without a successful
  `claude /login` against the new HOME, there's no oauth blob. The pool
  will discover this on first dispatch and mark the profile dead. The
  script's structure (scaffold → login → register) is mandatory.
- **Reusing an existing Gmail/email across profiles** — same Max
  account = same weekly quota, zero capacity gain. Always a fresh
  account.
- **Skipping the `.bashrc`/`.gitconfig`/`.npmrc` symlinks** — `claude`
  may invoke git/node and inherit broken config. The script handles
  this; don't reimplement without it.
- **Pointing `ANTHROPIC_BASE_URL` at a proxy globally** to "share"
  accounts — the pool rotation is the supported mechanism. Don't
  shortcut it.
- **Running this in CI / cron / background** — login is interactive,
  full stop. If invoked non-interactively, surface "needs operator at a
  terminal" and exit.

## What the script does NOT do

- It does not restart any agents — the pool is read fresh on each
  `claude` invocation, so the new profile is live immediately.
- It does not modify `~/.reusable-agents/secrets.env` or any framework
  config. The pool dir is self-contained.
- It does not buy a Max subscription — the operator must do that on
  claude.ai first; the script only registers an already-paid account.
