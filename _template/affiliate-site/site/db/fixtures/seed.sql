-- The deploy gate's fixture content. tests/run-local-suite.sh applies it
-- (SUITE_FIXTURE_FRESHEN_SQL in tests/suite.env) once the app under test is
-- healthy, so every migration of the commit under test has run. FIXTURE DATA
-- ONLY: never run it against a production database.
--
-- Idempotent: ON CONFLICT keeps rows that already exist, so the long-lived
-- fixture database can take it on every gate run.
--
-- Once the site installs the kit's publish guard (content-policy sites; see
-- vendor/affiliate-kit/migrations/README.md), an INSERT by this fixture role
-- is forced to the draft status with published_at NULL. Seed published
-- articles then through SET ROLE <publisher role>, never replica mode, and
-- set published_at in that same UPDATE: a promote that sets only status
-- leaves a live article with no publication date.
DO $seed$
BEGIN
  IF to_regclass('public.editorial_articles') IS NULL THEN
    RAISE EXCEPTION 'seed: editorial_articles does not exist: the app under test did not apply the kit migrations';
  END IF;
END
$seed$;

-- One published guide with a GFM table and an FAQ: the gate's article family,
-- its table rule and the content-policy scan all need a real article to read.
INSERT INTO editorial_articles (slug, title, subtitle, excerpt, body_md, category, status, published_at, faqs)
VALUES (
  'how-we-compare-products',
  'How We Compare Products',
  'Where the specifications, prices and safety notes on this site come from',
  'How this site compares products: specifications from the makers, prices checked on Amazon.com with the time shown, and recalls from public notices.',
  $md$Every product page and guide on this site is built from the same three sources.

## What we check

| What we check | Where it comes from |
|---|---|
| Specifications | The maker's spec sheets and manuals |
| Prices and availability | Amazon.com, with the time we checked |
| Recalls | Public recall notices |

## How a guide is put together

We start from what a reader needs the product to do, then compare the models that can do it on the
specifications that matter for that job. A model with missing or conflicting specifications is left out
until the maker's own documents settle it.

Read [about this site](/about) for who writes the guides, and [how we earn money](/disclosure) for the
Amazon Associates disclosure.
$md$,
  'guides',
  'published',
  now() - interval '2 days',
  '[{"question": "Where do the prices come from?", "answer": "From Amazon.com. Each price shows the time it was checked, and prices can change after that."}, {"question": "Do commissions change the price I pay?", "answer": "No. Amazon pays the commission; the price you pay is the same."}]'::jsonb
)
ON CONFLICT (slug) DO NOTHING;
