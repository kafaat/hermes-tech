-- =====================================================================
-- Hermes · 0005_v11_channels_quality_queue.sql
-- Additions from the v1.1 reference review:
--   1. channel_accounts   : official channel identities per customer (WhatsApp Cloud API
--                           phone_number_id, Facebook Page id...) = the webhook tenant router
--   2. webhook_events     : idempotent, signature-checked ingestion (replay-safe)
--   3. quality_flags      : findings of content_guard + agent_quality (silent mode first)
--   4. outbox             : transactional outbox for every external side effect
--   5. eval_runs          : results of offline evaluation suites (operators only)
--   6. app.claim_task()   : durable queue claim with FOR UPDATE SKIP LOCKED
--   7. tenant indexes     : btree index on customer_id for every tenant table (RLS speed)
-- =====================================================================
begin;

create type app.channel_kind   as enum ('whatsapp_cloud','facebook_page','instagram_business','site_form','email');
create type app.channel_status as enum ('pending_verification','active','suspended','revoked');
create type app.flag_severity  as enum ('review','block');
create type app.flag_status    as enum ('open','confirmed','dismissed');

-- ------------------------------------------------------------ 1. channel accounts
create table app.channel_accounts (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  kind            app.channel_kind not null,
  external_id     text not null,                      -- phone_number_id, page id, ...
  display_name    text,
  secret_ref_id   uuid references app.secret_refs(id),
  status          app.channel_status not null default 'pending_verification',
  verified_at     timestamptz,
  created_at      timestamptz not null default now(),
  unique (kind, external_id),                          -- one owner per external identity
  check (status <> 'active' or verified_at is not null)
);

-- ------------------------------------------------------------ 2. webhook events
create table app.webhook_events (
  id              bigint generated always as identity primary key,
  kind            app.channel_kind not null,
  external_event_id text not null,                     -- provider message/event id
  customer_id     uuid references app.customers(id),   -- resolved through channel_accounts
  signature_valid boolean not null,
  received_at     timestamptz not null default now(),
  processed_at    timestamptz,
  payload         jsonb not null,
  unique (kind, external_event_id),                    -- replay / duplicate delivery safe
  check (signature_valid or processed_at is null)      -- unsigned events are never processed
);

-- ------------------------------------------------------------ 3. quality flags
create table app.quality_flags (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  content_id      uuid references app.content_items(id),
  source          text not null check (source in ('content_guard','agent_quality')),
  rule_id         text not null,
  severity        app.flag_severity not null,
  detail          text,
  status          app.flag_status not null default 'open',
  resolved_by     uuid,
  resolved_at     timestamptz,
  created_at      timestamptz not null default now(),
  check (status = 'open' or (resolved_by is not null and resolved_at is not null))
);

-- ------------------------------------------------------------ 4. outbox
create table app.outbox (
  id              bigint generated always as identity primary key,
  customer_id     uuid references app.customers(id),
  topic           text not null check (topic ~ '^[a-z_]+\.[a-z_]+$'),
  payload         jsonb not null,
  approval_id     uuid references app.approvals(id),
  created_at      timestamptz not null default now(),
  dispatched_at   timestamptz,
  attempts        smallint not null default 0,
  last_error      text,
  check (topic not in ('content.publish','reply.send','site.deploy_prod') or approval_id is not null)
);
create index outbox_pending on app.outbox (created_at) where dispatched_at is null;

-- ------------------------------------------------------------ 5. eval runs
create table app.eval_runs (
  id              uuid primary key default gen_random_uuid(),
  suite           text not null,
  git_sha         text not null check (git_sha ~ '^[0-9a-f]{7,40}$'),
  metrics         jsonb not null,
  passed          boolean not null,
  ran_at          timestamptz not null default now()
);

-- ------------------------------------------------------------ RLS for the new tables
alter table app.channel_accounts enable row level security;
alter table app.channel_accounts force row level security;
create policy channel_accounts_owner_select on app.channel_accounts for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy channel_accounts_operator_all on app.channel_accounts for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy channel_accounts_worker_select on app.channel_accounts for select to hermes_worker using (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.channel_accounts to authenticated;
grant select on app.channel_accounts to hermes_worker;

alter table app.webhook_events enable row level security;
alter table app.webhook_events force row level security;
create policy webhook_events_operator_all on app.webhook_events for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy webhook_events_worker_rw on app.webhook_events for all to hermes_worker
  using (customer_id is not distinct from (select app.worker_customer_id()))
  with check (customer_id is not distinct from (select app.worker_customer_id()));
grant select, insert, update on app.webhook_events to hermes_worker, authenticated;

alter table app.quality_flags enable row level security;
alter table app.quality_flags force row level security;
create policy quality_flags_owner_select on app.quality_flags for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy quality_flags_operator_all on app.quality_flags for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy quality_flags_worker_rw on app.quality_flags for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.quality_flags to authenticated, hermes_worker;

alter table app.outbox enable row level security;
alter table app.outbox force row level security;
create policy outbox_operator_all on app.outbox for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy outbox_worker_rw on app.outbox for all to hermes_worker
  using (customer_id is not distinct from (select app.worker_customer_id()))
  with check (customer_id is not distinct from (select app.worker_customer_id()));
grant select, insert, update on app.outbox to authenticated, hermes_worker;

alter table app.eval_runs enable row level security;
alter table app.eval_runs force row level security;
create policy eval_runs_operator_all on app.eval_runs for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
grant select, insert on app.eval_runs to authenticated;

-- ------------------------------------------------------------ 6. durable queue claim
-- Workers call this inside their tenant-scoped transaction; SKIP LOCKED lets several
-- workers drain the same queue without double-claiming a task.
create or replace function app.claim_task(p_agent_id text)
returns app.tasks language plpgsql security invoker set search_path = app, pg_temp as $$
declare t app.tasks;
begin
  select * into t from app.tasks
   where status = 'queued' and agent_id = p_agent_id
   order by priority desc, created_at
   for update skip locked
   limit 1;
  if found then
    update app.tasks set status = 'running', started_at = now(), attempts = attempts + 1
     where id = t.id returning * into t;
  end if;
  return t;
end $$;
grant execute on function app.claim_task(text) to hermes_worker;

-- ------------------------------------------------------------ 7. tenant indexes (policy columns)
create index if not exists idx_subscriptions_customer   on app.subscriptions (customer_id);
create index if not exists idx_invoices_customer        on app.invoices (customer_id);
create index if not exists idx_payments_customer        on app.payments (customer_id);
create index if not exists idx_deployments_customer     on app.deployments (customer_id);
create index if not exists idx_media_assets_customer    on app.media_assets (customer_id);
create index if not exists idx_content_items_customer   on app.content_items (customer_id);
create index if not exists idx_kb_facts_customer        on app.kb_facts (customer_id);
create index if not exists idx_approvals_customer       on app.approvals (customer_id);
create index if not exists idx_competitor_snaps_customer on app.competitor_snapshots (customer_id);
create index if not exists idx_support_tickets_customer on app.support_tickets (customer_id);
create index if not exists idx_secret_refs_customer     on app.secret_refs (customer_id);
create index if not exists idx_tasks_customer           on app.tasks (customer_id);
create index if not exists idx_quality_flags_customer   on app.quality_flags (customer_id);
create index if not exists idx_channel_accounts_customer on app.channel_accounts (customer_id);

-- ------------------------------------------------------------ reporting view (agent scores as report, never as an automatic switch)
create view app.v_agent_monthly_report with (security_invoker = true) as
select c.agent_id, date_trunc('month', c.ts)::date as month,
       count(*)                                        as calls,
       round(avg((c.outcome = 'success')::int)::numeric, 4) as success_rate,
       sum(c.cost_usd)                                 as cost_usd,
       percentile_cont(0.95) within group (order by c.duration_ms) as p95_ms
from app.agent_calls c
group by 1, 2;
grant select on app.v_agent_monthly_report to authenticated;

commit;
