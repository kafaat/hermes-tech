-- =====================================================================
-- Hermes · 0001_core.sql
-- Target: Supabase Postgres 15+ (auth.uid() available). For plain
-- Postgres run db/local/0000_supabase_shim.sql first.
-- Every tenant table carries customer_id NOT NULL. Row-level security
-- is applied in 0002_rls.sql; nothing in this file is reachable by
-- application roles until 0002 grants and policies are in place.
-- =====================================================================
begin;

create extension if not exists pgcrypto;
create schema if not exists app;

-- ------------------------------------------------------------ enums
create type app.customer_status   as enum ('lead','pending_payment','active','suspended','expired','cancelled');
create type app.sector            as enum ('restaurant','home_services','retail','education','other');
create type app.user_role         as enum ('owner','staff');
create type app.operator_role     as enum ('founder','ops','tech','finance');
create type app.subscription_status as enum ('pending','active','expired','cancelled','refunded');
create type app.invoice_status    as enum ('draft','issued','paid','void','refunded');
create type app.payment_channel   as enum ('bank','wallet','exchange','cash');
create type app.payment_status    as enum ('pending','matched','rejected');
create type app.site_status       as enum ('draft','preview','live','frozen','archived');
create type app.deploy_env        as enum ('staging','prod');
create type app.deploy_status     as enum ('queued','building','deployed','failed','rolled_back');
create type app.content_kind      as enum ('post','reply','report');
create type app.content_status    as enum ('draft','pending_approval','approved','published','rejected');
create type app.media_kind        as enum ('image','video');
create type app.media_source      as enum ('generated','owner_upload','licensed');
create type app.approval_decision as enum ('pending','approved','rejected','expired');
create type app.task_status       as enum ('queued','running','succeeded','failed','escalated','cancelled');
create type app.call_outcome      as enum ('success','failure','escalated','rejected','budget_exceeded','circuit_open');
create type app.circuit_state     as enum ('closed','open','half_open');
create type app.inquiry_source    as enum ('site_form','whatsapp','facebook','instagram','tiktok','phone','other');
create type app.message_channel   as enum ('patron','client');
create type app.complaint_category as enum ('pricing','quality','legal','safety','refund');
create type app.ticket_status     as enum ('open','waiting_owner','resolved','closed');
create type app.time_activity     as enum ('onboarding','support','content_review','edit','sales_demo','outreach','renewal_call','maintenance','billing');
create type app.snapshot_status   as enum ('ok','unverifiable','blocked');
create type app.contact_status    as enum ('not_contacted','consented','contacted','declined','do_not_contact');
create type app.incident_severity as enum ('P0','P1','P2','P3');
create type app.actor_type        as enum ('user','operator','agent','system');

-- ------------------------------------------------------------ identity
create table app.customers (
  id              uuid primary key default gen_random_uuid(),
  public_ref      text not null unique check (public_ref ~ '^cust_[0-9a-z]{4,}$'),
  business_name   text not null,
  sector          app.sector not null,
  city            text not null,
  currency_zone   text not null check (currency_zone in ('zone_a','zone_b')),
  status          app.customer_status not null default 'lead',
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table app.customer_users (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id) on delete cascade,
  auth_user_id    uuid not null,
  role            app.user_role not null default 'owner',
  active          boolean not null default true,
  created_at      timestamptz not null default now(),
  unique (customer_id, auth_user_id)
);
create index on app.customer_users (auth_user_id) where active;

create table app.operators (
  auth_user_id    uuid primary key,
  display_name    text not null,
  role            app.operator_role not null,
  mfa_enrolled    boolean not null default false,
  active          boolean not null default true,
  created_at      timestamptz not null default now(),
  check (not active or mfa_enrolled)          -- no active operator without MFA
);

-- ------------------------------------------------------------ commercial
create table app.subscriptions (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  plan_code       text not null default 'base_annual',
  price_usd       numeric(10,2) not null check (price_usd > 0),
  period_start    date not null,
  period_end      date not null,
  status          app.subscription_status not null default 'pending',
  renewal_of      uuid references app.subscriptions(id),
  created_at      timestamptz not null default now(),
  check (period_end > period_start)
);
create unique index subscriptions_one_active on app.subscriptions (customer_id) where status = 'active';

create table app.invoices (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  subscription_id uuid references app.subscriptions(id),
  number          text not null unique,
  amount          numeric(12,2) not null check (amount > 0),
  currency        text not null check (currency in ('USD','YER','SAR')),
  fx_rate_to_usd  numeric(14,6),
  status          app.invoice_status not null default 'draft',
  issued_at       timestamptz,
  paid_at         timestamptz,
  created_at      timestamptz not null default now(),
  check (currency = 'USD' or fx_rate_to_usd is not null)
);

create table app.payments (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  invoice_id      uuid not null references app.invoices(id),
  channel         app.payment_channel not null,
  receipt_ref     text not null,
  amount          numeric(12,2) not null check (amount > 0),
  currency        text not null,
  fee_amount      numeric(12,2) not null default 0,
  received_at     timestamptz not null,
  status          app.payment_status not null default 'pending',
  verified_by     uuid references app.operators(auth_user_id),
  verified_at     timestamptz,
  created_at      timestamptz not null default now(),
  unique (channel, receipt_ref),                               -- one receipt, one payment
  check (status <> 'matched' or (verified_by is not null and verified_at is not null))  -- human verification
);

-- ------------------------------------------------------------ sites and releases
create table app.templates (
  code            text not null,
  version         text not null check (version ~ '^\d+\.\d+\.\d+$'),
  min_schema      int not null,
  max_schema      int not null,
  released_at     timestamptz,
  yanked          boolean not null default false,
  primary key (code, version),
  check (max_schema >= min_schema)
);

create table app.sites (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  template_code   text not null,
  pinned_version  text not null,
  domain          text,
  preview_url     text,
  prod_url        text,
  status          app.site_status not null default 'draft',
  last_deploy_at  timestamptz,
  foreign key (template_code, pinned_version) references app.templates(code, version),
  unique (customer_id)
);

create table app.deployments (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  site_id         uuid not null references app.sites(id),
  template_version text not null,
  env             app.deploy_env not null,
  content_hash    text not null,
  status          app.deploy_status not null default 'queued',
  approval_id     uuid,                                         -- required for prod (see check below)
  rollback_of     uuid references app.deployments(id),
  created_at      timestamptz not null default now(),
  check (env = 'staging' or approval_id is not null or rollback_of is not null)
);

-- ------------------------------------------------------------ content and media
create table app.media_assets (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  r2_key          text not null unique,
  kind            app.media_kind not null,
  source          app.media_source not null,
  license_note    text,
  bytes           bigint not null check (bytes > 0),
  created_at      timestamptz not null default now(),
  check (source <> 'licensed' or license_note is not null)
);

create table app.content_items (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  week_id         text not null check (week_id ~ '^\d{4}-W\d{2}$'),
  kind            app.content_kind not null,
  body            text not null,
  media_ids       uuid[] not null default '{}',
  platform        text check (platform in ('facebook','instagram','site','whatsapp')),
  status          app.content_status not null default 'draft',
  approval_id     uuid,
  published_at    timestamptz,
  created_at      timestamptz not null default now(),
  check (status <> 'published' or approval_id is not null)      -- nothing published without approval
);

create table app.kb_facts (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  topic           text not null,
  fact            text not null,
  approved_by_owner boolean not null default false,
  valid_until     date,
  updated_at      timestamptz not null default now()
);

-- ------------------------------------------------------------ approvals (proposals executed by humans)
create table app.approvals (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  proposal_action text not null check (proposal_action ~ '^[a-z_]+:[a-z_]+$'),
  payload         jsonb not null,
  requested_by_agent text not null,
  requested_at    timestamptz not null default now(),
  expires_at      timestamptz not null default now() + interval '7 days',
  decision        app.approval_decision not null default 'pending',
  decided_by      uuid,
  decided_at      timestamptz,
  executed_by     uuid,
  executed_at     timestamptz,
  check (decision = 'pending' or (decided_by is not null and decided_at is not null)),
  check (executed_at is null or decision = 'approved')
);
alter table app.deployments  add foreign key (approval_id) references app.approvals(id);
alter table app.content_items add foreign key (approval_id) references app.approvals(id);

-- ------------------------------------------------------------ orchestration
create table app.tasks (
  id              uuid primary key default gen_random_uuid(),
  public_ref      text not null unique check (public_ref ~ '^t_[0-9a-z]+$'),
  customer_id     uuid references app.customers(id),            -- null only for acquisition tasks
  agent_id        text not null check (agent_id ~ '^agent_[a-z][a-z0-9_]{2,31}$'),
  kind            text not null,
  status          app.task_status not null default 'queued',
  priority        smallint not null default 2 check (priority between 0 and 3),
  idempotency_key text not null unique,
  attempts        smallint not null default 0,
  error_code      text,
  created_at      timestamptz not null default now(),
  started_at      timestamptz,
  finished_at     timestamptz
);
create index tasks_queue on app.tasks (status, priority desc, created_at) where status in ('queued','running');

create table app.agent_calls (
  id              bigint generated always as identity primary key,
  ts              timestamptz not null default now(),
  task_id         uuid not null references app.tasks(id),
  customer_id     uuid references app.customers(id),
  agent_id        text not null,
  tool            text not null,
  model           text,
  tokens_in       int not null default 0 check (tokens_in >= 0),
  tokens_out      int not null default 0 check (tokens_out >= 0),
  cost_usd        numeric(10,5) not null default 0 check (cost_usd >= 0),
  duration_ms     int not null check (duration_ms >= 0),
  outcome         app.call_outcome not null,
  error_code      text,
  idempotency_key text not null
);
create index agent_calls_customer_ts on app.agent_calls (customer_id, ts);
create index agent_calls_agent_ts    on app.agent_calls (agent_id, ts);

create table app.agent_state (
  customer_id     uuid not null references app.customers(id),
  agent_id        text not null,
  month_id        text not null check (month_id ~ '^\d{4}-\d{2}$'),
  calls           int not null default 0,
  spent_usd       numeric(10,5) not null default 0,
  consecutive_failures smallint not null default 0,
  circuit         app.circuit_state not null default 'closed',
  circuit_opened_at timestamptz,
  alert_fired     boolean not null default false,
  primary key (customer_id, agent_id, month_id)
);

-- ------------------------------------------------------------ competitors
create table app.competitors (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  url             text not null check (url ~ '^https://'),
  label           text not null,
  active          boolean not null default true,
  unique (customer_id, url)
);
create unique index competitors_label on app.competitors (customer_id, label) where active;  -- max two active: trigger in 0003

create table app.competitor_snapshots (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  competitor_id   uuid not null references app.competitors(id),
  fetched_at      timestamptz not null default now(),
  content_hash    text,
  diff_summary    text,
  status          app.snapshot_status not null,
  check (status <> 'ok' or content_hash is not null)
);

-- ------------------------------------------------------------ inquiries, tickets, time (measurement for gates 1.5, 1.6, 2.3)
create table app.inquiries (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  source          app.inquiry_source not null,
  received_at     timestamptz not null default now(),
  body            text,                                          -- purged after 30 days (retention job)
  matched_category app.complaint_category,
  owner_inquiry   boolean not null default false,
  routed_to       text[] not null default '{}',
  handled_at      timestamptz
);
create index inquiries_customer_time on app.inquiries (customer_id, received_at);

create table app.support_tickets (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  channel         app.message_channel not null default 'client',
  category        app.complaint_category,
  severity        text check (severity in ('high','critical')),
  status          app.ticket_status not null default 'open',
  opened_at       timestamptz not null default now(),
  first_response_at timestamptz,
  resolved_at     timestamptz
);

create table app.time_entries (
  id              uuid primary key default gen_random_uuid(),
  operator_id     uuid not null references app.operators(auth_user_id),
  customer_id     uuid references app.customers(id),
  activity        app.time_activity not null,
  minutes         int not null check (minutes > 0 and minutes <= 600),
  occurred_at     timestamptz not null default now()
);

-- ------------------------------------------------------------ acquisition (no tenant)
create table app.leads (
  id              uuid primary key default gen_random_uuid(),
  place_id        text unique,                                   -- only Places field stored
  business_name   text not null,
  city            text not null,
  sector          app.sector not null,
  source_url      text not null,
  verified_at     timestamptz not null,
  contact_status  app.contact_status not null default 'not_contacted',
  consent_ref     text,
  qualification_note text,
  check (contact_status not in ('contacted') or consent_ref is not null)
);

-- ------------------------------------------------------------ secrets (pointers only, never values)
create table app.secret_refs (
  id              uuid primary key default gen_random_uuid(),
  customer_id     uuid not null references app.customers(id),
  provider        text not null,
  vault_ref       text not null check (vault_ref ~ '^vault://'),
  scopes          text[] not null,
  rotated_at      timestamptz not null default now(),
  unique (customer_id, provider)
);

-- ------------------------------------------------------------ operations
create table app.incidents (
  id              text primary key check (id ~ '^INC-\d{4}-\d{2}-\d{2}-\d{3}$'),
  severity        app.incident_severity not null,
  category        text not null check (category in ('circuit','queue','budget','leak','vendor','other')),
  affected_customers uuid[] not null default '{}',
  reported_at     timestamptz not null default now(),
  contained_at    timestamptz,
  mitigated_at    timestamptz,
  diagnosed_at    timestamptz,
  fixed_at        timestamptz,
  closed_at       timestamptz,
  root_cause      text,
  prevention      text,
  check (closed_at is null or (root_cause is not null and prevention is not null))
);

create table app.gate_measurements (
  id              uuid primary key default gen_random_uuid(),
  gate_id         text not null check (gate_id ~ '^[0-3]\.[0-9]{1,2}[أبت]?$'),
  measured_at     timestamptz not null default now(),
  value_num       numeric,
  value_text      text,
  source          text not null,
  note            text,
  check (value_num is not null or value_text is not null)
);

create table app.audit_log (
  id              bigint generated always as identity primary key,
  ts              timestamptz not null default now(),
  actor_type      app.actor_type not null,
  actor_id        text not null,
  customer_id     uuid references app.customers(id),
  action          text not null,
  target          text,
  details         jsonb not null default '{}',
  prev_hash       text,
  hash            text
);

commit;
