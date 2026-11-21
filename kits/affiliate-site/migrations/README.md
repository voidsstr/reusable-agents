# Kit migrations

The tables, views and triggers every affiliate site shares, applied before the
site's own migrations by one runner under one advisory lock and one ledger.

| File | Creates |
|---|---|
| `kit_0001_outbound_clicks.sql` | `outbound_clicks` (20 columns) and its indexes |
| `kit_0002_ai_traffic_log.sql` | `ai_traffic_log` (14 columns) and its indexes |
| `kit_0003_editorial_articles.sql` | `editorial_articles` (34 columns), its indexes, the hero-image CHECKs and the updated_at content trigger |
| `kit_0004_views.sql` | `kit_click_row_verdict()`, `outbound_clicks_verdicts`, `outbound_clicks_human`, `outbound_clicks_verified_human`, `amazon_clicks_verified_human_daily` |
| `templates/publish_trigger.sql.tmpl` | the editorial_articles publish guard, rendered per site (not applied by itself) |

## Column contract

Columns, order, types, nullability, defaults and index names are identical to
the reference deployment's live tables (checked against its information_schema
on 2026-11-17; `unit/db/fixtures/upstream-schema.json`), so every framework
reader and the ARTICLE_AUTHOR INSERT work unchanged. The deliberate
differences:

- `editorial_articles.author` defaults to `'Editorial'`, not a site name. Set
  the site's default from `site.json` `byline.name` in a site migration.
- `hero_image_url` carries two CHECKs.
  - `editorial_articles_hero_image_no_amazon_cdn` bans every Amazon-hosted
    image: an image CDN host anywhere in the URL (`m.media-amazon.com`,
    `*.ssl-images-amazon.com`, `*.images-amazon.com`, `amazon-adsystem.com`,
    `assoc-amazon.com`, or a mirror path named after one), and an
    `amazon.<tld>` storefront host or short-link host (`amzn.to`, `a.co`, ...)
    followed by a path, port or query, which covers
    `images.amazon.com/images/P/<ASIN>...` and `www.amazon.com/images/I/...`.
    Never relax it.
  - `editorial_articles_hero_image_host_allowlist` accepts NULL, `''`, or,
    with no whitespace or control character anywhere (browsers drop tab, CR
    and LF from a URL, so `/<TAB>/evil.example/x` loads from another host), a
    site-relative path that is not protocol-relative, or https on an Azure
    blob account. Any account passes, and anyone can create one: a site that
    knows its storage account pins it by replacing this constraint in a site
    migration (`DROP CONSTRAINT` + `ADD CONSTRAINT`), as does a site that
    serves heroes from elsewhere. The Amazon ban stays.
- The reference site's category-alias and vertical triggers are niche values
  and are not here.
- The verified-human views use the full `framework/core/human_clicks.py`
  verdict (below), with the reference views' column lists.

`unit/db/drift.test.ts` reports when the reference site adds a migration that
touches these tables or views. Mirror it in a new kit migration.

## Applying them

Run the migrations as the database's **owner role**, never from the app's
runtime pool: whoever runs them owns every table they create, and a table's
owner can disable its triggers, the publish guard's included. The kit's deploy
(`deploy/README.md`) does this in its `migrate` stage, which runs
`MIGRATE_COMMAND` inside the new image with the owner DSN as `DATABASE_URL`:

```bash
MIGRATE_COMMAND="node <kit>/dist/db/migrate.js --site-dir <site migrations dir>"
```

`--status` lists applied and pending files without changing anything. The
DSN comes from `$DATABASE_URL` (or `--dsn-env NAME`), never from an argument.
The app then boots on its least-privilege pool and can refuse to serve on a
schema that is behind:

```ts
import { kitMigrationSources, pendingMigrations } from '../vendor/affiliate-kit/server/src/db';
const s = await pendingMigrations(pool, { sources: kitMigrationSources({ siteMigrationsDir }) });
if (s.pending.length) throw new Error(`pending migrations: ${s.pending.map((f) => f.name).join(', ')}`);
```

A site without a content policy may instead apply them at boot, before
`app.listen`, with `await runKitMigrations(pool, { siteMigrationsDir })` (a
failure throws, so the replica exits and restarts rather than serving a
half-migrated schema). That makes the app role the owner of every table.

- Order: every pending kit file in name order, then every pending site file.
- Ledger: `_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMP)`, keyed by
  file name, so a name may exist in only one source and site files may not
  start with `kit_`.
- Lock: session advisory lock `1835624306`, polled for up to 60 s and taken
  only when something is pending; the ledger is re-read under it, so replicas
  booting together apply each file once. To apply a file by hand, run
  `SELECT pg_try_advisory_lock(1835624306)` in the same session first and keep
  it until the file and its `_migrations` row are both committed.
- Each file is sent as one query, so it runs as one implicit transaction
  (`SET LOCAL` holds for the file). A file whose SQL (not its comments or
  strings) says `CONCURRENTLY` runs statement by statement instead.
- A `RAISE WARNING` from a file reaches the boot log.

## Writing a kit migration

- Never edit a file a database has applied: add `kit_NNNN_<what>.sql`.
- Start with `SET LOCAL lock_timeout = '5s';` then
  `SET LOCAL statement_timeout = '15min';`. Every site role has its own
  statement_timeout, and a file cancelled by a short one stops the boot; each
  statement after the `SET LOCAL` gets the 15 minutes.
- Use only idempotent forms: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
  EXISTS`, `CREATE OR REPLACE FUNCTION | VIEW | TRIGGER`, `COMMENT ON`, guarded
  `DO` blocks. No `CONCURRENTLY`, no `BEGIN`/`COMMIT`, no data changes.
- No site names outside comments. Dollar-quote regexes (`$re$...$re$`).
- `unit/db/kitSchema.test.ts` enforces these.

## The verified-human views

`kit_click_row_verdict()` is human_clicks.py's per-row rule set with its
DEFAULT_SPEC (site-flagged-bot, no-ua, ua-bot, stale-browser, excluded-country,
datacenter-ip, no-referer, scanner-referer, in that order), and
`outbound_clicks_verdicts` adds its velocity cut: more than 20 rows passing
those rules from one IP on one UTC day, across every via. The goal series is
`outbound_clicks_verified_human` (beacon rows only).
`unit/db/humanClicksConsistency.test.ts` diffs every constant in the latest
kit migration that defines the function and the view against human_clicks.py,
and `unit/db/views.pg.test.ts` compares the verdicts row by row on a real
database. A change to human_clicks.py's defaults goes into a new kit migration
that replaces both.

Overrides in storage `config/human-click-filter-config.json` (a Safari floor,
extra prefixes) apply only to human_clicks.py, so goal metrics come from it.
To change a constant for one site, replace `kit_click_row_verdict()` in a site
migration with the same signature.

The velocity window reads every row the query reaches. Filter
`outbound_clicks_verdicts` on `click_day`, which reaches the table scan, not
on `clicked_at`, which does not.

## The publish guard (content_policy sites)

A site that opts into a content policy installs the guard as one of its own
migrations:

```bash
node <kit>/dist/db/publishTrigger.js --publisher-role <site>_publisher --draft-status pending --out db/migrations/0009_publish_guard.sql
```

or `renderPublishTrigger({ publisherRole, draftStatus })` from
`server/src/db`. `--draft-status` must be the publish gate's
`publish_gate.draft_status` (the gate's default is `pending`; the template's is
`draft`), or the gate refuses to write. For every role except the publisher:

- an INSERT is always the draft status, with `published_at` NULL whatever the
  author wrote: the publish gate stamps `published_at` when it first promotes
  the row (`COALESCE(published_at, now())`), so a draft held for days goes
  live dated the day it went live;
- an UPDATE asking for `published` on a row that was not published keeps the
  status the row had: a row in the gate's queue (the draft status) stays
  queued, and a held row (`archived`, or `draft` when the queue is `pending`)
  stays held. Only a write that asks for the draft status itself resubmits a
  held row;
- no UPDATE clears a `published_at` once set, so an edited or resubmitted
  article keeps its first go-live date. Setting another time is allowed
  (`published_at` is not content), which is how a fixture re-dates its
  articles;
- an edit to a published row's content sends it back to the draft status for
  the publish gate to re-check. The content columns are what the gate's
  checks read: title, subtitle, excerpt, body_md, faqs, outbound_citations and
  category (which picks the per-bucket claims and link policies); a site that
  adds a `bucket` column lists it too;
- other updates of a published row (a `view_count` bump on every page view,
  hero swaps, featured rotation, trend scores, link arrays) keep it published;
- leaving the published state (`archived`, `draft`) is always allowed.

An author's upsert (`INSERT ... ON CONFLICT (slug) DO UPDATE`) takes the
UPDATE rules on its conflict path. Leave `status` out of its `DO UPDATE` list,
or ask for `published` there: `status = EXCLUDED.status` asks for the draft
status, because `EXCLUDED` carries what the INSERT rule forced, and that
resubmits a held row. For the same reason `published_at = EXCLUDED.published_at`
sets NULL, and the guard keeps the row's own date instead.

It installs two triggers, both `ENABLE ALWAYS`, so
`session_replication_role = replica` does not skip them:

- `editorial_articles_zzz_publish_guard_trg` (BEFORE INSERT OR UPDATE) applies
  the rules. BEFORE triggers fire in name order, bytewise: **name every other
  BEFORE trigger on editorial_articles so it sorts before this one**
  (`editorial_articles_<...>` does; `set_*` and `trg_*` do not).
- `editorial_articles_zzz_publish_recheck_trg` (AFTER, rows stored as
  published) raises when the row as written breaks the rules anyway, for
  instance because a later-sorting BEFORE trigger changed it, when a write by
  the publisher changed its content, and when the publisher writes while any
  trigger on the table runs a function that neither the table owner (or a
  member of it) nor a superuser owns. Such a write fails instead of publishing
  unchecked content. It is a `NOT DEFERRABLE` constraint trigger: `CREATE OR
  REPLACE TRIGGER`, which needs only the `TRIGGER` privilege, cannot swap it,
  and dropping, renaming or disabling it needs the table owner. Constraint
  triggers take no `OR REPLACE`, so applying the migration drops and creates
  it again (an `ACCESS EXCLUSIVE` lock on the table until the migration
  commits; the migration's 5 s `lock_timeout` bounds the wait).

`framework/cli/article_publish_gate.py` promotes drafts through a separate
connection as the publisher role. A promote that returns the draft status
tells it the connection is not that role (`trigger-kept-draft`). For the guard
to mean anything (installing it warns about the first three it can see):

- no role but the table owner holds `TRIGGER` on `editorial_articles`.
  `GRANT ALL`, `ALTER DEFAULT PRIVILEGES ... GRANT ALL` and membership of the
  owner role all include it. A role that can create a trigger can make its own
  writes pass the BEFORE guard, or replace the guard outright (`CREATE OR
  REPLACE TRIGGER`), and can plant code that runs as whoever writes the row
  next, the publisher included. The recheck stops all of those from
  publishing: it judges the row as written, and it refuses every publish while
  a trigger runs a function that neither the owner (or a member of it) nor a
  superuser owns. But until such a trigger is gone nothing can be published,
  and a guard replaced by a temporary function vanishes when its session ends:
  from then on the writes the guard would have turned into drafts fail, and
  the publish gate refuses to run, until this migration's guard is applied
  again. Grant `SELECT`, `INSERT`, `UPDATE` by name; the kit's deploy
  provisioning does. When a publish fails this way, the error names each
  trigger with its function and that function's owner: for a trigger of the
  site's own, `ALTER FUNCTION ... OWNER TO` the table owner (after the table
  changes owner, the guard and recheck functions too); otherwise drop the
  trigger and revoke `TRIGGER`;
- no BEFORE trigger sorts after the guard (above);
- the author and app roles cannot `SET session_replication_role`
  (`GRANT SET ON PARAMETER`, PostgreSQL 15+, or a managed admin group that
  has it). `pg_restore --disable-triggers` and `ALTER TABLE ... ENABLE
  TRIGGER` reset both triggers to fire on origin only; afterwards run
  `ALTER TABLE editorial_articles ENABLE ALWAYS TRIGGER
  editorial_articles_zzz_publish_guard_trg, ENABLE ALWAYS TRIGGER
  editorial_articles_zzz_publish_recheck_trg`. Seed fixtures promote through
  `SET ROLE <publisher>`, never replica mode, and set `published_at` in that
  same UPDATE (`SET status = 'published', published_at = ...`): the fixture's
  own INSERT left it NULL, and a promote that sets only `status` leaves a live
  article with no publication date;
- the author and app roles are not the publisher role, not members of it, and
  not superusers;
- migrations run as a dedicated owner role, and neither the author role nor
  the app role owns `editorial_articles` or is a member of its owner (an owner
  can disable triggers; see "Applying them");
- only the publish gate's process holds the publisher DSN. It is never in an
  environment or a file an author session can read: keep it out of the
  agents' shared secrets file, or the author whose INSERTs keep coming back as
  drafts can find it and connect as the publisher;
- no `SECURITY DEFINER` function owned by the publisher role writes
  `editorial_articles` (inside one, `current_user` is its owner);
- the publisher role needs only `SELECT` and `UPDATE (status, published_at)`.

If the role does not exist yet, the migration warns and nothing can be
published until it does. A content column is compared by name on every write
to a published row, so after one is dropped or renamed those writes fail with
an error, the page-view counter's included, instead of passing or unpublishing
the article; re-render the guard in the migration that changes the column. To
change a parameter, render again into a new migration. The table needs
`status` and `published_at` as well as the content columns, or the
installation fails. A full `pg_restore` creates the triggers after loading the
rows; a data-only restore fires them on every row, which turns every article
into a draft with `published_at` NULL, and the gate would then re-date every
article as it re-publishes it. Restore those with `--disable-triggers`, then
re-enable as above.

## Tests

`./test.sh` runs the offline suites. The `unit/db/*.pg.test.ts` suites run
only when `KIT_TEST_DATABASE_URL` points at a throwaway server's superuser.
They create and drop databases and roles there, so they refuse (fail before
any statement) a host that is not loopback unless
`KIT_TEST_DATABASE_ALLOW_REMOTE=1`, and a server holding any database other
than `postgres`, the templates and their own `kit_*` scratch databases unless
`KIT_TEST_DATABASE_ALLOW_SHARED=1`. For example (the password stays out of
every command line):

```bash
umask 077
printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 18)" > kit-pg.env
docker run -d --rm --name kit-pg --env-file kit-pg.env -p 127.0.0.1:55740:5432 postgres:16
KIT_TEST_DATABASE_URL="postgres://postgres:$(sed -n 's/^POSTGRES_PASSWORD=//p' kit-pg.env)@127.0.0.1:55740/postgres" npx jest unit/db
```

Each of those suites creates its own database and roles and drops them
afterwards.
