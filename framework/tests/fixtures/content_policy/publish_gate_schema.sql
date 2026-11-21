-- Test double of the content-policy site schema the publish gate runs against
-- (framework/tests/test_article_publish_gate.py, real-Postgres part). The
-- canonical trigger is the kit's template
-- (kits/affiliate-site/migrations/templates/publish_trigger.sql.tmpl, build
-- item K4); this copy follows the same rules, so the gate is exercised against
-- the contract K4 ships (and the composition test renders K4's own template):
--   * any INSERT by a role other than the publisher lands in the gate's
--     queue, 'pending', with published_at NULL (a self-INSERTed 'published'
--     row never goes live, and the gate stamps the first go-live time);
--   * an UPDATE by another role that asks for 'published' on a row that was
--     not published keeps the status it had: a 'pending' row stays queued
--     and a hold ('draft', 'archived') stays a hold until a write asks for
--     'pending' itself;
--   * an UPDATE by another role that changes the checked text of a published
--     row lands as 'pending' (the gate re-checks it);
--   * any other UPDATE keeps the status it asks for: leaving 'published'
--     (a takedown to 'draft' or 'archived') is always allowed, and
--     housekeeping writes to a published row (a hero image, a view count)
--     keep it published;
--   * no UPDATE by another role clears a published_at once set.
-- {author} and {publisher} are replaced with the test's role names.
CREATE TABLE editorial_articles (
  id            SERIAL PRIMARY KEY,
  slug          VARCHAR(200) UNIQUE NOT NULL,
  title         VARCHAR(300) NOT NULL,
  subtitle      VARCHAR(500),
  excerpt       TEXT,
  body_md       TEXT NOT NULL,
  hero_image_url TEXT,
  category      VARCHAR(50) NOT NULL,
  tags          TEXT[] NOT NULL DEFAULT '{}',
  related_product_asins TEXT[] NOT NULL DEFAULT '{}',
  author        VARCHAR(100) NOT NULL DEFAULT 'Editorial',
  status        VARCHAR(20) NOT NULL DEFAULT 'draft',
  faqs          JSONB,
  written_by    TEXT,
  written_at    TIMESTAMPTZ,
  published_at  TIMESTAMPTZ,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE FUNCTION editorial_articles_publish_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  publisher     CONSTANT name := '{publisher}';
  draft_status  CONSTANT text := 'pending';
  public_status CONSTANT text := 'published';
BEGIN
  IF current_user = publisher THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'INSERT' THEN
    NEW.status := draft_status;
    NEW.published_at := NULL;
    RETURN NEW;
  END IF;
  IF NEW.published_at IS NULL AND OLD.published_at IS NOT NULL THEN
    NEW.published_at := OLD.published_at;
  END IF;
  IF NEW.status IS DISTINCT FROM public_status THEN
    RETURN NEW;
  END IF;
  IF OLD.status IS DISTINCT FROM public_status THEN
    NEW.status := OLD.status;
    RETURN NEW;
  END IF;
  IF (NEW.body_md, NEW.title, NEW.subtitle, NEW.excerpt, NEW.faqs)
     IS DISTINCT FROM (OLD.body_md, OLD.title, OLD.subtitle, OLD.excerpt, OLD.faqs) THEN
    NEW.status := draft_status;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER editorial_articles_zzz_publish_guard_trg
  BEFORE INSERT OR UPDATE ON editorial_articles
  FOR EACH ROW EXECUTE FUNCTION editorial_articles_publish_guard();

GRANT SELECT, INSERT, UPDATE ON editorial_articles TO {author};
GRANT SELECT, UPDATE (status, published_at) ON editorial_articles TO {publisher};
GRANT USAGE ON SEQUENCE editorial_articles_id_seq TO {author};
