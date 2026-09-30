-- 0017 · competitor snapshots keep the structured facts, and a daily job writes them
-- A snapshot now holds, besides the hash and the owner's summary: the facts extracted from the page's schema.org
-- JSON-LD (items, prices, hours, rating; service/structured.py), needed to compute the next difference, and a hash of
-- the page's visible text, to say "the page changed but publishes no structured data". Neither is the page: the
-- retention rule "hash and summary only, not the page" (spec 4.4) now reads "…and the extracted facts".
--   status ok            facts extracted; content_hash is the facts' hash; diff_summary the computed difference
--   status unverifiable  fetched, no structured facts (or the fetch failed); page_hash says whether the page changed
--   status blocked       the site refused the check: robots.txt or a login page
-- The daily job (service/competitor.py) runs as hermes_jobs: it reads active competitors (no owner data beyond the
-- URL the owner chose) and their snapshots, and inserts snapshots only for the competitor's own customer, at most
-- 10 per customer per calendar month (registry package_limits.competitor_checks_per_month), enforced here.
begin;

alter table app.competitor_snapshots
  add column structured_facts jsonb check (structured_facts is null or octet_length(structured_facts::text) <= 65536),
  add column page_hash text check (page_hash is null or page_hash ~ '^[0-9a-f]{64}$');

grant select (id, customer_id, url, label, active) on app.competitors to hermes_jobs;
create policy competitors_jobs_read on app.competitors for select to hermes_jobs using (active);

grant select (customer_id, competitor_id, fetched_at, status, content_hash, structured_facts, page_hash)
  on app.competitor_snapshots to hermes_jobs;
grant insert (customer_id, competitor_id, content_hash, diff_summary, status, structured_facts, page_hash)
  on app.competitor_snapshots to hermes_jobs;
create policy competitor_snapshots_jobs_read on app.competitor_snapshots for select to hermes_jobs using (true);
create policy competitor_snapshots_jobs_insert on app.competitor_snapshots for insert to hermes_jobs
  with check (customer_id = (select c.customer_id from app.competitors c where c.id = competitor_id and c.active)
              and (select count(*) from app.competitor_snapshots s
                    where s.customer_id = competitor_snapshots.customer_id
                      and s.fetched_at >= date_trunc('month', now())) < 10);

commit;
