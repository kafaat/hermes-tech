-- =====================================================================
-- Hermes · 0006_v12_hardening.sql · response to the v1.1 review
--   1. EXECUTE on every function in schema app revoked from PUBLIC; explicit
--      re-grants only (security definer inventory: docs/security_definer_inventory.md)
--   2. leads: business names never come from Google Maps content (caching terms);
--      place_id may be stored indefinitely and is refreshed every 12 months
--   3. secret rotation: 90-day rule surfaced as a view (alert: secret_rotation_overdue)
--   4. restore drills: mandatory monthly record + status view (alert: restore_drill_overdue)
-- =====================================================================
begin;

-- ------------------------------------------------------------ 1. function privileges
revoke execute on all functions in schema app from public;
alter default privileges in schema app revoke execute on functions from public;
grant execute on function app.current_user_customer_ids() to authenticated;
grant execute on function app.is_operator()               to authenticated;
grant execute on function app.worker_customer_id()        to hermes_worker, authenticated;
grant execute on function app.claim_task(text)            to hermes_worker;
-- guard triggers call this as the invoking role, so the invoker needs EXECUTE
grant execute on function app.assert_approved(uuid, uuid, text) to hermes_worker, authenticated;
-- app.audit_verify(): no grant; run by operators through the service role only.
-- Trigger functions need no EXECUTE grant to fire.

-- ------------------------------------------------------------ 2. leads provenance
create type app.lead_name_source as enum ('owner_site','facebook_page','manual_visit','business_registry');
alter table app.leads add column name_source app.lead_name_source;
alter table app.leads add column place_id_refreshed_at timestamptz;
alter table app.leads add constraint leads_name_source_required check (name_source is not null) not valid;
alter table app.leads add constraint leads_place_id_refresh check (place_id is null or place_id_refreshed_at is not null) not valid;
comment on column app.leads.name_source is
  'Where business_name was read. Google Maps content is never stored except place_id (exempt from caching restrictions).';

create view app.v_place_ids_to_refresh with (security_invoker = true) as
select id, place_id, place_id_refreshed_at from app.leads
where place_id is not null and place_id_refreshed_at < now() - interval '12 months';

-- ------------------------------------------------------------ 3. secret rotation
create view app.v_secrets_rotation_due with (security_invoker = true) as
select s.customer_id, s.provider, s.rotated_at, (now() - s.rotated_at) as age
from app.secret_refs s
where s.rotated_at < now() - interval '90 days';

-- ------------------------------------------------------------ 4. restore drills
create table app.restore_drills (
  id              uuid primary key default gen_random_uuid(),
  drilled_at      timestamptz not null default now(),
  backup_taken_at timestamptz not null,
  target          text not null check (target in ('scratch_project','local_postgres')),
  duration_minutes int not null check (duration_minutes > 0),
  rows_verified   boolean not null,
  audit_chain_ok  boolean not null,
  rls_test_passed boolean not null,
  passed          boolean generated always as (rows_verified and audit_chain_ok and rls_test_passed and duration_minutes <= 480) stored,
  performed_by    uuid not null references app.operators(auth_user_id),
  notes           text,
  check (backup_taken_at <= drilled_at)
);
alter table app.restore_drills enable row level security;
alter table app.restore_drills force row level security;
create policy restore_drills_operator_all on app.restore_drills for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
grant select, insert on app.restore_drills to authenticated;

create view app.v_restore_drill_status with (security_invoker = true) as
select max(drilled_at) filter (where passed)                         as last_passed_at,
       extract(day from now() - max(drilled_at) filter (where passed)) as days_since_last_passed,
       max(extract(epoch from drilled_at - backup_taken_at) / 3600) filter (where passed) as worst_rpo_hours
from app.restore_drills;

grant select on app.v_place_ids_to_refresh, app.v_secrets_rotation_due, app.v_restore_drill_status to authenticated;
commit;
