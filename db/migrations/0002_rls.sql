-- =====================================================================
-- Hermes · 0002_rls.sql · roles, helper functions, row-level security
-- Model:
--   authenticated  : portal users (business owners and operators) via Supabase Auth
--   hermes_worker  : orchestrator and agent workers. NO bypassrls. Must set
--                    app.customer_id per transaction; sees exactly one tenant.
--   service_role   : migrations and break-glass only. Never used by agents.
-- FORCE ROW LEVEL SECURITY on every table except the identity lookups
-- (customer_users, operators) and audit_log, whose owner-executed helper
-- functions and trigger must read them without recursion. Those three keep
-- ENABLE RLS with policies that do not call the helper functions.
-- Stable helpers are wrapped as (select fn()) so the planner evaluates them once per
-- statement (initPlan) instead of once per row (Supabase RLS performance guidance).
-- =====================================================================
begin;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_worker') then
    create role hermes_worker nologin noinherit;
  end if;
end $$;

grant usage on schema app to authenticated, hermes_worker;

-- ------------------------------------------------------------ helper functions
create or replace function app.current_user_customer_ids() returns setof uuid
  language sql stable security definer set search_path = app, pg_temp as $$
  select customer_id from app.customer_users where auth_user_id = auth.uid() and active
$$;

create or replace function app.is_operator() returns boolean
  language sql stable security definer set search_path = app, pg_temp as $$
  select exists (select 1 from app.operators where auth_user_id = auth.uid() and active)
$$;

create or replace function app.worker_customer_id() returns uuid
  language sql stable set search_path = app, pg_temp as $$
  select nullif(current_setting('app.customer_id', true), '')::uuid
$$;

revoke all on function app.current_user_customer_ids() from public;
revoke all on function app.is_operator() from public;
grant execute on function app.current_user_customer_ids(), app.is_operator() to authenticated;
grant execute on function app.worker_customer_id() to hermes_worker, authenticated;

-- ------------------------------------------------------------ identity (ENABLE only; see header)
alter table app.customer_users enable row level security;
create policy customer_users_self      on app.customer_users for select to authenticated using (auth_user_id = (select auth.uid()));
create policy customer_users_operator  on app.customer_users for all    to authenticated
  using (exists (select 1 from app.operators o where o.auth_user_id = (select auth.uid()) and o.active))
  with check (exists (select 1 from app.operators o where o.auth_user_id = (select auth.uid()) and o.active));
grant select, insert, update on app.customer_users to authenticated;

alter table app.operators enable row level security;
create policy operators_self on app.operators for select to authenticated using (auth_user_id = (select auth.uid()));
grant select on app.operators to authenticated;

-- ------------------------------------------------------------ customers (tenant root: id is the tenant key)
alter table app.customers enable row level security;
alter table app.customers force row level security;
create policy customers_owner_select on app.customers for select to authenticated using (id in (select app.current_user_customer_ids()));
create policy customers_operator_all on app.customers for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy customers_worker_select on app.customers for select to hermes_worker using (id = (select app.worker_customer_id()));
grant select, insert, update on app.customers to authenticated;
grant select on app.customers to hermes_worker;

-- ------------------------------------------------------------ subscriptions
alter table app.subscriptions enable row level security;
alter table app.subscriptions force row level security;
create policy subscriptions_owner_select on app.subscriptions for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy subscriptions_operator_all on app.subscriptions for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy subscriptions_worker_rw on app.subscriptions for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.subscriptions to hermes_worker;
grant select, insert, update on app.subscriptions to authenticated;

-- ------------------------------------------------------------ invoices
alter table app.invoices enable row level security;
alter table app.invoices force row level security;
create policy invoices_owner_select on app.invoices for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy invoices_operator_all on app.invoices for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy invoices_worker_rw on app.invoices for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.invoices to hermes_worker;
grant select, insert, update on app.invoices to authenticated;

-- ------------------------------------------------------------ payments
alter table app.payments enable row level security;
alter table app.payments force row level security;
create policy payments_owner_select on app.payments for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy payments_operator_all on app.payments for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy payments_worker_select on app.payments for select to hermes_worker using (customer_id = (select app.worker_customer_id()));
grant select on app.payments to hermes_worker;
grant select, insert, update on app.payments to authenticated;

-- ------------------------------------------------------------ sites
alter table app.sites enable row level security;
alter table app.sites force row level security;
create policy sites_owner_select on app.sites for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy sites_operator_all on app.sites for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy sites_worker_rw on app.sites for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.sites to hermes_worker;
grant select, insert, update on app.sites to authenticated;

-- ------------------------------------------------------------ deployments
alter table app.deployments enable row level security;
alter table app.deployments force row level security;
create policy deployments_owner_select on app.deployments for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy deployments_operator_all on app.deployments for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy deployments_worker_rw on app.deployments for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.deployments to hermes_worker;
grant select, insert, update on app.deployments to authenticated;

-- ------------------------------------------------------------ media_assets
alter table app.media_assets enable row level security;
alter table app.media_assets force row level security;
create policy media_assets_owner_select on app.media_assets for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy media_assets_operator_all on app.media_assets for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy media_assets_worker_rw on app.media_assets for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.media_assets to hermes_worker;
grant select, insert, update on app.media_assets to authenticated;

-- ------------------------------------------------------------ content_items
alter table app.content_items enable row level security;
alter table app.content_items force row level security;
create policy content_items_owner_select on app.content_items for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy content_items_operator_all on app.content_items for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy content_items_worker_rw on app.content_items for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.content_items to hermes_worker;
grant select, insert, update on app.content_items to authenticated;

-- ------------------------------------------------------------ kb_facts
alter table app.kb_facts enable row level security;
alter table app.kb_facts force row level security;
create policy kb_facts_owner_select on app.kb_facts for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy kb_facts_operator_all on app.kb_facts for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy kb_facts_worker_rw on app.kb_facts for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.kb_facts to hermes_worker;
grant select, insert, update on app.kb_facts to authenticated;

-- ------------------------------------------------------------ approvals
alter table app.approvals enable row level security;
alter table app.approvals force row level security;
create policy approvals_owner_select on app.approvals for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy approvals_operator_all on app.approvals for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy approvals_worker_rw on app.approvals for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.approvals to hermes_worker;
grant select, insert, update on app.approvals to authenticated;

-- ------------------------------------------------------------ agent_state
alter table app.agent_state enable row level security;
alter table app.agent_state force row level security;
create policy agent_state_operator_all on app.agent_state for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy agent_state_worker_rw on app.agent_state for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.agent_state to hermes_worker;
grant select, insert, update on app.agent_state to authenticated;

-- ------------------------------------------------------------ competitors
alter table app.competitors enable row level security;
alter table app.competitors force row level security;
create policy competitors_owner_select on app.competitors for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy competitors_operator_all on app.competitors for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy competitors_worker_rw on app.competitors for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.competitors to hermes_worker;
grant select, insert, update on app.competitors to authenticated;

-- ------------------------------------------------------------ competitor_snapshots
alter table app.competitor_snapshots enable row level security;
alter table app.competitor_snapshots force row level security;
create policy competitor_snapshots_owner_select on app.competitor_snapshots for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy competitor_snapshots_operator_all on app.competitor_snapshots for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy competitor_snapshots_worker_rw on app.competitor_snapshots for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.competitor_snapshots to hermes_worker;
grant select, insert, update on app.competitor_snapshots to authenticated;

-- ------------------------------------------------------------ inquiries
alter table app.inquiries enable row level security;
alter table app.inquiries force row level security;
create policy inquiries_owner_select on app.inquiries for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy inquiries_operator_all on app.inquiries for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy inquiries_worker_rw on app.inquiries for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.inquiries to hermes_worker;
grant select, insert, update on app.inquiries to authenticated;

-- ------------------------------------------------------------ support_tickets
alter table app.support_tickets enable row level security;
alter table app.support_tickets force row level security;
create policy support_tickets_owner_select on app.support_tickets for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy support_tickets_operator_all on app.support_tickets for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy support_tickets_worker_rw on app.support_tickets for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.support_tickets to hermes_worker;
grant select, insert, update on app.support_tickets to authenticated;

-- ------------------------------------------------------------ secret_refs
alter table app.secret_refs enable row level security;
alter table app.secret_refs force row level security;
create policy secret_refs_operator_all on app.secret_refs for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy secret_refs_worker_select on app.secret_refs for select to hermes_worker using (customer_id = (select app.worker_customer_id()));
grant select on app.secret_refs to hermes_worker;
grant select, insert, update on app.secret_refs to authenticated;

-- ------------------------------------------------------------ owner decisions on approvals (the only owner write path)
create policy approvals_owner_decide on app.approvals for update to authenticated
  using (customer_id in (select app.current_user_customer_ids()) and decision = 'pending' and expires_at > now())
  with check (customer_id in (select app.current_user_customer_ids()) and decided_by = (select auth.uid()) and decision in ('approved','rejected'));

-- ------------------------------------------------------------ tasks and agent_calls (nullable customer_id = acquisition context)
alter table app.tasks enable row level security;
alter table app.tasks force row level security;
create policy tasks_operator_all on app.tasks for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy tasks_worker_rw on app.tasks for all to hermes_worker
  using (customer_id is not distinct from (select app.worker_customer_id()))
  with check (customer_id is not distinct from (select app.worker_customer_id()));
grant select, insert, update on app.tasks to hermes_worker, authenticated;

alter table app.agent_calls enable row level security;
alter table app.agent_calls force row level security;
create policy agent_calls_operator_select on app.agent_calls for select to authenticated using ((select app.is_operator()));
create policy agent_calls_worker_insert on app.agent_calls for insert to hermes_worker
  with check (customer_id is not distinct from (select app.worker_customer_id()));
create policy agent_calls_worker_select on app.agent_calls for select to hermes_worker
  using (customer_id is not distinct from (select app.worker_customer_id()));
grant select on app.agent_calls to authenticated;
grant select, insert on app.agent_calls to hermes_worker;

-- ------------------------------------------------------------ acquisition and operator-only tables
alter table app.leads enable row level security;
alter table app.leads force row level security;
create policy leads_operator_all on app.leads for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy leads_worker_acquisition on app.leads for all to hermes_worker
  using ((select app.worker_customer_id()) is null) with check ((select app.worker_customer_id()) is null and contact_status = 'not_contacted');
grant select, insert, update on app.leads to authenticated, hermes_worker;

alter table app.templates enable row level security;
alter table app.templates force row level security;
create policy templates_read on app.templates for select to authenticated, hermes_worker using (true);
create policy templates_operator_write on app.templates for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
grant select on app.templates to authenticated, hermes_worker;
grant insert, update on app.templates to authenticated;

alter table app.time_entries enable row level security;
alter table app.time_entries force row level security;
create policy time_entries_operator_all on app.time_entries for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
grant select, insert, update on app.time_entries to authenticated;

alter table app.incidents enable row level security;
alter table app.incidents force row level security;
create policy incidents_operator_all on app.incidents for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
grant select, insert, update on app.incidents to authenticated;

alter table app.gate_measurements enable row level security;
alter table app.gate_measurements force row level security;
create policy gate_measurements_operator_all on app.gate_measurements for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
grant select, insert on app.gate_measurements to authenticated;

-- no delete grants anywhere: deletions go through reviewed operator procedures only
commit;
