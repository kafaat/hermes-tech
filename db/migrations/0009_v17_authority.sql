-- =====================================================================
-- Hermes · 0009_v17_authority.sql · the authority layer (independent review, P1 01–05 and 09)
--   01 the worker can only PROPOSE: no decision, decider, consumption or owner-approval columns
--   02 outbox: closed topic list; a matching, approved, unconsumed approval is verified and
--      consumed in the database; payload immutable after insert; re-verified at dispatch
--   03 approvals bind target_id + payload_hash; single use; published content immutable;
--      prod deploy checked on INSERT and UPDATE; rollback must restore an approved artifact
--   04 the worker's tenant is derived from a leased task (token), not from a GUC it chooses;
--      composite (id, customer_id) foreign keys on sensitive links
--   05 operator authority requires a session at aal2, not just MFA enrolment
--   09 task leases with heartbeat, fencing token, recovery and dead-letter
--   plus: audit chain hashed with a timezone-independent timestamp and ordered by chain_seq
-- =====================================================================
begin;

-- ------------------------------------------------------------ 05 operator = active operator AND aal2 session
create or replace function app.jwt_aal() returns text
  language sql stable security definer set search_path = app, pg_temp as $$
  select coalesce(auth.jwt()->>'aal', '')
$$;
create or replace function app.is_operator() returns boolean
  language sql stable security definer set search_path = app, pg_temp as $$
  select exists (select 1 from app.operators where auth_user_id = auth.uid() and active)
     and app.jwt_aal() = 'aal2'
$$;
grant execute on function app.jwt_aal() to authenticated;
-- the one policy that inlined the operator check now uses the central function
drop policy customer_users_operator on app.customer_users;
create policy customer_users_operator on app.customer_users for all to authenticated
  using ((select app.is_operator())) with check ((select app.is_operator()));

-- ------------------------------------------------------------ 04/09 task leases: the worker's only source of tenant
create sequence app.fencing_seq;
create table app.task_leases (
  task_id         uuid primary key references app.tasks(id),
  customer_id     uuid references app.customers(id),
  agent_id        text not null,
  token           uuid not null,
  worker          text not null,
  fencing         bigint not null,
  lease_until     timestamptz not null,
  heartbeat_at    timestamptz not null default now()
);
alter table app.task_leases enable row level security;      -- ENABLE only: read and written by the definer functions below
create policy task_leases_operator_select on app.task_leases for select to authenticated using ((select app.is_operator()));
grant select on app.task_leases to authenticated;            -- no grant to hermes_worker: leases change only through the functions below

-- The tenant is whatever the caller's LIVE lease says. A worker that sets app.customer_id itself gets nothing.
create or replace function app.worker_customer_id() returns uuid
  language sql stable security definer set search_path = app, pg_temp as $$
  select l.customer_id from app.task_leases l
   where l.task_id = nullif(current_setting('app.task_id', true), '')::uuid
     and l.token   = nullif(current_setting('app.task_token', true), '')::uuid
     and l.lease_until > now()
$$;
create or replace function app.worker_context() returns text
  language sql stable security definer set search_path = app, pg_temp as $$
  select case when l.task_id is null then 'none' when l.customer_id is null then 'acquisition' else 'customer' end
    from (select 1) one
    left join app.task_leases l
      on l.task_id = nullif(current_setting('app.task_id', true), '')::uuid
     and l.token   = nullif(current_setting('app.task_token', true), '')::uuid
     and l.lease_until > now()
$$;
grant execute on function app.worker_context() to hermes_worker, authenticated;

drop function app.claim_task(text);
create function app.claim_task(p_agent_id text, p_worker text, p_lease_seconds int default 300, p_max_attempts int default 3)
returns table (task_id uuid, token uuid, fencing bigint, customer_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
#variable_conflict use_column
declare t app.tasks; tok uuid := gen_random_uuid(); f bigint;
begin
  -- recovery: running tasks whose lease expired go back to the queue, or to the dead letter after max attempts
  update app.tasks x set status = case when x.attempts >= p_max_attempts then 'failed'::app.task_status else 'queued'::app.task_status end,
         error_code = case when x.attempts >= p_max_attempts then 'DEAD_LETTER' else x.error_code end,
         run_after = now()
   where x.status = 'running' and x.agent_id = p_agent_id
     and exists (select 1 from app.task_leases l where l.task_id = x.id and l.lease_until < now());
  select * into t from app.tasks q
   where q.status = 'queued' and q.agent_id = p_agent_id and q.run_after <= now()
   order by q.priority desc, q.created_at
   for update skip locked limit 1;
  if not found then return; end if;
  update app.tasks set status = 'running', started_at = now(), attempts = attempts + 1 where id = t.id;
  f := nextval('app.fencing_seq');
  insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
  values (t.id, t.customer_id, t.agent_id, tok, p_worker, f, now() + make_interval(secs => p_lease_seconds))
  on conflict (task_id) do update set token = excluded.token, worker = excluded.worker, fencing = excluded.fencing,
     lease_until = excluded.lease_until, heartbeat_at = now(), customer_id = excluded.customer_id;
  return query select t.id, tok, f, t.customer_id;
end $$;

create or replace function app.bind_task(p_task uuid, p_token uuid) returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare c uuid;
begin
  if not exists (select 1 from app.task_leases where task_id = p_task and token = p_token and lease_until > now()) then
    raise exception 'LEASE_INVALID' using errcode = '42501';
  end if;
  perform set_config('app.task_id', p_task::text, true), set_config('app.task_token', p_token::text, true);
  select customer_id into c from app.task_leases where task_id = p_task;
  return c;
end $$;

create or replace function app.extend_task_lease(p_task uuid, p_token uuid, p_seconds int) returns boolean
language sql security definer set search_path = app, pg_temp as $$
  with u as (update app.task_leases set lease_until = now() + make_interval(secs => p_seconds), heartbeat_at = now()
              where task_id = p_task and token = p_token and lease_until > now() returning 1)
  select exists (select 1 from u);
$$;

-- Fencing: only the current holder can finish the task; a worker whose lease was taken over cannot.
create or replace function app.complete_task(p_task uuid, p_token uuid, p_fencing bigint, p_status app.task_status, p_error text default null)
returns boolean language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not exists (select 1 from app.task_leases where task_id = p_task and token = p_token and fencing = p_fencing) then
    return false;
  end if;
  update app.tasks set status = p_status, error_code = p_error, finished_at = now() where id = p_task;
  update app.task_leases set lease_until = now() where task_id = p_task;
  return true;
end $$;

drop function app.requeue_task(uuid, int);
create function app.requeue_task(p_task uuid, p_token uuid, p_delay_seconds int) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not exists (select 1 from app.task_leases where task_id = p_task and token = p_token and lease_until > now()) then
    return false;
  end if;
  update app.tasks set status = 'queued', run_after = now() + make_interval(secs => greatest(p_delay_seconds, 1)) where id = p_task;
  update app.task_leases set lease_until = now() where task_id = p_task;
  return true;
end $$;

grant execute on function app.claim_task(text, text, int, int), app.bind_task(uuid, uuid), app.extend_task_lease(uuid, uuid, int),
  app.complete_task(uuid, uuid, bigint, app.task_status, text), app.requeue_task(uuid, uuid, int) to hermes_worker;

-- Policies that allowed rows with a NULL customer to any worker: acquisition rows now need an acquisition lease.
drop policy tasks_worker_rw on app.tasks;
create policy tasks_worker_rw on app.tasks for all to hermes_worker
  using (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'))
  with check (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'));
drop policy agent_calls_worker_insert on app.agent_calls;
drop policy agent_calls_worker_select on app.agent_calls;
create policy agent_calls_worker_insert on app.agent_calls for insert to hermes_worker
  with check (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'));
create policy agent_calls_worker_select on app.agent_calls for select to hermes_worker
  using (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'));
drop policy outbox_worker_rw on app.outbox;
create policy outbox_worker_rw on app.outbox for all to hermes_worker
  using (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'))
  with check (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'));
drop policy task_budget_worker_rw on app.task_budget;
create policy task_budget_worker_rw on app.task_budget for all to hermes_worker
  using (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'))
  with check (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'));
drop policy idempotency_keys_worker_rw on app.idempotency_keys;
create policy idempotency_keys_worker_rw on app.idempotency_keys for all to hermes_worker
  using (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'))
  with check (customer_id = (select app.worker_customer_id()) or (customer_id is null and (select app.worker_context()) = 'acquisition'));
drop policy leads_worker_acquisition on app.leads;
create policy leads_worker_acquisition on app.leads for all to hermes_worker
  using ((select app.worker_context()) = 'acquisition')
  with check ((select app.worker_context()) = 'acquisition' and contact_status = 'not_contacted');
drop policy acquisition_budget_worker_rw on app.acquisition_budget;
create policy acquisition_budget_worker_rw on app.acquisition_budget for all to hermes_worker
  using ((select app.worker_context()) = 'acquisition') with check ((select app.worker_context()) = 'acquisition');
drop policy exceed_events_worker_rw on app.reservation_exceed_events;
create policy exceed_events_worker_rw on app.reservation_exceed_events for all to hermes_worker
  using ((select app.worker_context()) <> 'none') with check ((select app.worker_context()) <> 'none');
drop policy agent_pauses_worker_pause on app.agent_pauses;
create policy agent_pauses_worker_pause on app.agent_pauses for insert to hermes_worker
  with check ((select app.worker_context()) <> 'none' and reason = 'RESERVATION_EXCEEDED' and released_at is null);

-- Webhook ingestion is not a worker: a dedicated role inserts events, a definer function routes them.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_ingest') then create role hermes_ingest nologin noinherit; end if;
end $$;
grant usage on schema app to hermes_ingest;
drop policy webhook_events_worker_rw on app.webhook_events;
create policy webhook_events_worker_select on app.webhook_events for select to hermes_worker
  using (customer_id = (select app.worker_customer_id()));
create policy webhook_events_worker_process on app.webhook_events for update to hermes_worker
  using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
create policy webhook_events_ingest_insert on app.webhook_events for insert to hermes_ingest
  with check (customer_id is null and processed_at is null);
revoke insert on app.webhook_events from hermes_worker;
grant insert (kind, external_event_id, signature_valid, payload) on app.webhook_events to hermes_ingest;

-- Tables that definer functions read or write lose FORCE (as in 0002 for the identity lookups): the
-- application never connects as the table owner, and app roles stay fully subject to their policies.
alter table app.tasks no force row level security;          -- claim_task / complete_task / requeue_task
alter table app.approvals no force row level security;      -- outbox_before_write consumes approvals

-- ------------------------------------------------------------ 04 composite ownership on sensitive links
alter table app.sites          add constraint sites_id_customer_uq unique (id, customer_id);
alter table app.invoices       add constraint invoices_id_customer_uq unique (id, customer_id);
alter table app.subscriptions  add constraint subscriptions_id_customer_uq unique (id, customer_id);
alter table app.content_items  add constraint content_items_id_customer_uq unique (id, customer_id);
alter table app.secret_refs    add constraint secret_refs_id_customer_uq unique (id, customer_id);
alter table app.competitors    add constraint competitors_id_customer_uq unique (id, customer_id);
alter table app.deployments          add constraint deployments_site_same_customer foreign key (site_id, customer_id) references app.sites (id, customer_id);
alter table app.payments             add constraint payments_invoice_same_customer foreign key (invoice_id, customer_id) references app.invoices (id, customer_id);
alter table app.invoices             add constraint invoices_subscription_same_customer foreign key (subscription_id, customer_id) references app.subscriptions (id, customer_id);
alter table app.quality_flags        add constraint quality_flags_content_same_customer foreign key (content_id, customer_id) references app.content_items (id, customer_id);
alter table app.channel_accounts     add constraint channel_accounts_secret_same_customer foreign key (secret_ref_id, customer_id) references app.secret_refs (id, customer_id);
alter table app.competitor_snapshots add constraint snapshots_competitor_same_customer foreign key (competitor_id, customer_id) references app.competitors (id, customer_id);

-- ------------------------------------------------------------ 01/03 approvals: scope, binding, single use, who decides
alter table app.approvals alter column customer_id drop not null;
alter table app.approvals add column scope text not null default 'customer' check (scope in ('customer','platform'));
alter table app.approvals add column target_id text;
alter table app.approvals add column payload_hash text;
alter table app.approvals add column consumed_at timestamptz;
alter table app.approvals add column consumed_by_ref text;
alter table app.approvals add constraint approvals_scope_customer check ((scope = 'customer') = (customer_id is not null));
alter table app.approvals add constraint approvals_target_required check (target_id is not null) not valid;
alter table app.approvals add constraint approvals_consumed_once check (consumed_at is null or (decision = 'approved' and consumed_by_ref is not null));
-- a reply must still be sendable as a free-form message after the owner decides (WhatsApp 24 h service window)
alter table app.approvals add constraint approvals_reply_window check (proposal_action <> 'reply:send' or expires_at <= requested_at + interval '20 hours') not valid;

create or replace function app.canonical_hash(p jsonb) returns text
  language sql immutable set search_path = app, extensions, public, pg_temp as $$
  select encode(digest(p::text, 'sha256'), 'hex')
$$;
create or replace function app.content_payload(c app.content_items) returns jsonb
  language sql immutable as $$
  select jsonb_build_object('content_id', c.id, 'body', c.body, 'media_ids', to_jsonb(c.media_ids), 'platform', c.platform)
$$;
create or replace function app.deploy_payload(d app.deployments) returns jsonb
  language sql immutable as $$
  select jsonb_build_object('site_id', d.site_id, 'content_hash', d.content_hash, 'template_version', d.template_version)
$$;

create or replace function app.approvals_before_write() returns trigger
language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.decision <> 'pending' or new.decided_by is not null or new.decided_at is not null or new.consumed_at is not null then
      raise exception 'PROPOSAL_MUST_BE_PENDING' using errcode = 'P0001';
    end if;
    new.payload_hash := app.canonical_hash(new.payload);
    return new;
  end if;
  if (new.payload, new.target_id, new.proposal_action, new.customer_id, new.scope, new.requested_by_agent, new.requested_at, new.expires_at)
     is distinct from (old.payload, old.target_id, old.proposal_action, old.customer_id, old.scope, old.requested_by_agent, old.requested_at, old.expires_at) then
    raise exception 'APPROVAL_IMMUTABLE' using errcode = 'P0001';
  end if;
  new.payload_hash := old.payload_hash;
  if old.decision <> 'pending' and new.decision is distinct from old.decision then
    raise exception 'DECISION_FINAL' using errcode = 'P0001';
  end if;
  if old.decision = 'pending' and new.decision in ('approved','rejected') then
    if old.expires_at <= now() then raise exception 'APPROVAL_EXPIRED' using errcode = 'P0001'; end if;
    if new.decided_by is distinct from auth.uid() or auth.uid() is null then raise exception 'DECIDER_MUST_BE_SESSION_USER' using errcode = '42501'; end if;
    if old.scope = 'customer' and not exists (select 1 from app.customer_users u
                                               where u.customer_id = old.customer_id and u.auth_user_id = auth.uid() and u.active) then
      raise exception 'CUSTOMER_APPROVAL_OWNER_ONLY' using errcode = '42501';     -- the founder cannot approve for a business
    end if;
    if old.scope = 'platform' and not app.is_operator() then
      raise exception 'PLATFORM_APPROVAL_OPERATOR_AAL2_ONLY' using errcode = '42501';
    end if;
    new.decided_at := now();
  end if;
  if new.consumed_at is distinct from old.consumed_at and coalesce(current_setting('app.consuming', true), '') <> 'on' then
    raise exception 'CONSUME_VIA_OUTBOX_ONLY' using errcode = '42501';   -- defence in depth: no role holds UPDATE on consumed_at
  end if;
  return new;
end $$;
create trigger approvals_before_write before insert or update on app.approvals
  for each row execute function app.approvals_before_write();

drop policy approvals_worker_rw on app.approvals;
drop policy approvals_operator_all on app.approvals;
create policy approvals_worker_select on app.approvals for select to hermes_worker
  using (customer_id = (select app.worker_customer_id()) or (scope = 'platform' and (select app.worker_context()) = 'acquisition'));
create policy approvals_worker_propose on app.approvals for insert to hermes_worker
  with check ((customer_id = (select app.worker_customer_id()) and scope = 'customer')
           or (customer_id is null and scope = 'platform' and (select app.worker_context()) = 'acquisition'));
create policy approvals_operator_select on app.approvals for select to authenticated using ((select app.is_operator()));
create policy approvals_operator_decide_platform on app.approvals for update to authenticated
  using ((select app.is_operator()) and scope = 'platform') with check ((select app.is_operator()) and scope = 'platform');
revoke insert, update on app.approvals from hermes_worker;
grant insert (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at) on app.approvals to hermes_worker;
revoke insert, update on app.approvals from authenticated;
grant update (decision, decided_by) on app.approvals to authenticated;

-- kb_facts: the worker drafts facts; only an owner of that business can mark one approved; any edit resets approval
revoke insert, update on app.kb_facts from hermes_worker;
grant insert (customer_id, topic, fact, valid_until) on app.kb_facts to hermes_worker;
grant update (topic, fact, valid_until) on app.kb_facts to hermes_worker;
create policy kb_facts_owner_approve on app.kb_facts for update to authenticated
  using (customer_id in (select app.current_user_customer_ids())) with check (customer_id in (select app.current_user_customer_ids()));
create or replace function app.kb_facts_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if tg_op = 'UPDATE' and (new.topic, new.fact, new.valid_until) is distinct from (old.topic, old.fact, old.valid_until) then
    new.approved_by_owner := false;                    -- a changed fact is a new fact
  end if;
  if new.approved_by_owner and (tg_op = 'INSERT' or not old.approved_by_owner) then
    if not exists (select 1 from app.customer_users u where u.customer_id = new.customer_id and u.auth_user_id = auth.uid() and u.active) then
      raise exception 'KB_APPROVAL_OWNER_ONLY' using errcode = '42501';
    end if;
  end if;
  new.updated_at := now();
  return new;
end $$;
create trigger kb_facts_guard before insert or update on app.kb_facts for each row execute function app.kb_facts_guard();

grant execute on function app.canonical_hash(jsonb), app.content_payload(app.content_items), app.deploy_payload(app.deployments)
  to hermes_worker, authenticated;

-- ------------------------------------------------------------ 02 outbox: closed topics, verified and consumed approval, immutable payload
create table app.outbox_topics (
  topic             text primary key,
  requires_approval boolean not null,
  proposal_action   text,
  scope             text not null check (scope in ('customer','platform')),
  check (requires_approval = (proposal_action is not null))
);
insert into app.outbox_topics values
  ('content.publish',  true,  'content:publish',  'customer'),
  ('reply.send',       true,  'reply:send',       'customer'),
  ('site.deploy_prod', true,  'site:deploy_prod', 'customer'),
  ('message.send',     true,  'message:send',     'platform'),
  ('lead.export',      true,  'lead:export',      'platform'),
  ('notify.owner',     false, null,               'customer');   -- platform notice to the owner, not an act on the business's behalf
alter table app.outbox_topics enable row level security;    -- ENABLE only: reference data read by the outbox definer trigger
create policy outbox_topics_read on app.outbox_topics for select to authenticated, hermes_worker using (true);
grant select on app.outbox_topics to authenticated, hermes_worker;

alter table app.outbox drop constraint if exists outbox_check;
alter table app.outbox add column target_id text;
alter table app.outbox add column payload_hash text;
alter table app.outbox add constraint outbox_topic_closed foreign key (topic) references app.outbox_topics (topic);
create unique index outbox_approval_single_use on app.outbox (approval_id) where approval_id is not null;

create or replace function app.outbox_before_write() returns trigger
language plpgsql security definer set search_path = app, extensions, public, pg_temp as $$
declare t app.outbox_topics; a app.approvals;
begin
  if tg_op = 'UPDATE' then
    if (new.topic, new.payload, new.approval_id, new.customer_id, new.target_id) is distinct from
       (old.topic, old.payload, old.approval_id, old.customer_id, old.target_id) then
      raise exception 'OUTBOX_IMMUTABLE' using errcode = 'P0001';
    end if;
    new.payload_hash := old.payload_hash;
    return new;
  end if;
  new.payload_hash := app.canonical_hash(new.payload);
  select * into t from app.outbox_topics where topic = new.topic;
  if not found then raise exception 'OUTBOX_TOPIC_UNKNOWN' using errcode = 'P0001'; end if;
  if not t.requires_approval then return new; end if;
  select * into a from app.approvals where id = new.approval_id for update;
  if not found then raise exception 'APPROVAL_NOT_FOUND' using errcode = 'P0001'; end if;
  if a.decision <> 'approved' then raise exception 'APPROVAL_NOT_APPROVED' using errcode = 'P0001'; end if;
  if a.expires_at <= now() then raise exception 'APPROVAL_EXPIRED' using errcode = 'P0001'; end if;
  if a.consumed_at is not null then raise exception 'APPROVAL_ALREADY_CONSUMED' using errcode = 'P0001'; end if;
  if a.proposal_action <> t.proposal_action or a.scope <> t.scope then raise exception 'APPROVAL_ACTION_MISMATCH' using errcode = 'P0001'; end if;
  if a.customer_id is distinct from new.customer_id then raise exception 'APPROVAL_TENANT_MISMATCH' using errcode = 'P0001'; end if;
  if a.target_id is distinct from new.target_id then raise exception 'APPROVAL_TARGET_MISMATCH' using errcode = 'P0001'; end if;
  if a.payload_hash <> new.payload_hash then raise exception 'APPROVAL_PAYLOAD_MISMATCH' using errcode = 'P0001'; end if;
  perform set_config('app.consuming', 'on', true);
  update app.approvals set consumed_at = now(), consumed_by_ref = 'outbox:' || new.id where id = a.id;
  perform set_config('app.consuming', '', true);
  return new;
end $$;
create trigger outbox_before_write before insert or update on app.outbox for each row execute function app.outbox_before_write();

-- Dispatch re-verifies the approval and takes a lease on the row atomically.
create or replace function app.claim_outbox_dispatch(p_id bigint, p_lease_seconds int default 120) returns boolean
language plpgsql security invoker set search_path = app, pg_temp as $$
declare o app.outbox; a app.approvals; t app.outbox_topics;
begin
  select * into o from app.outbox where id = p_id for update skip locked;
  if not found or o.dispatched_at is not null or o.needs_human_check
     or (o.sending_until is not null and o.sending_until > now()) then return false; end if;
  select * into t from app.outbox_topics where topic = o.topic;
  if t.requires_approval then
    select * into a from app.approvals where id = o.approval_id;
    if a.decision <> 'approved' or a.consumed_by_ref <> 'outbox:' || o.id or a.payload_hash <> o.payload_hash then
      update app.outbox set needs_human_check = true, last_error = 'APPROVAL_REVERIFY_FAILED' where id = o.id;
      return false;
    end if;
  end if;
  update app.outbox set sending_until = now() + make_interval(secs => p_lease_seconds), attempts = attempts + 1 where id = o.id;
  return true;
end $$;
grant execute on function app.claim_outbox_dispatch(bigint, int) to hermes_worker;

-- ------------------------------------------------------------ 03 published content and prod deploys bound to their approval
create or replace function app.guard_content_publish() returns trigger
language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
declare a app.approvals;
begin
  if tg_op = 'UPDATE' and old.status = 'published'
     and (new.body, new.media_ids, new.platform, new.approval_id) is distinct from (old.body, old.media_ids, old.platform, old.approval_id) then
    raise exception 'PUBLISHED_CONTENT_IMMUTABLE' using errcode = 'P0001';
  end if;
  if new.status = 'published' and (tg_op = 'INSERT' or old.status is distinct from 'published') then
    select * into a from app.approvals where id = new.approval_id;
    if not found then raise exception 'APPROVAL_NOT_FOUND' using errcode = 'P0001'; end if;
    if a.decision <> 'approved' or a.proposal_action <> 'content:publish' or a.customer_id <> new.customer_id
       or a.target_id <> new.id::text or a.payload_hash <> app.canonical_hash(app.content_payload(new))
       or a.consumed_at is null then
      raise exception 'APPROVAL_DOES_NOT_COVER_THIS_CONTENT' using errcode = 'P0001';
    end if;
    new.published_at := coalesce(new.published_at, now());
  end if;
  return new;
end $$;

create or replace function app.guard_prod_deploy() returns trigger
language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
declare a app.approvals; r app.deployments;
begin
  if tg_op = 'UPDATE' and (new.env, new.site_id, new.content_hash, new.template_version, new.approval_id, new.rollback_of, new.customer_id)
     is distinct from (old.env, old.site_id, old.content_hash, old.template_version, old.approval_id, old.rollback_of, old.customer_id) then
    raise exception 'DEPLOYMENT_IMMUTABLE' using errcode = 'P0001';
  end if;
  if tg_op = 'INSERT' and new.env = 'prod' then
    if new.rollback_of is null then
      select * into a from app.approvals where id = new.approval_id;
      if not found or a.decision <> 'approved' or a.proposal_action <> 'site:deploy_prod' or a.customer_id <> new.customer_id
         or a.target_id <> new.site_id::text or a.payload_hash <> app.canonical_hash(app.deploy_payload(new)) or a.consumed_at is null then
        raise exception 'APPROVAL_DOES_NOT_COVER_THIS_DEPLOY' using errcode = 'P0001';
      end if;
    else
      if not app.is_operator() then raise exception 'ROLLBACK_OPERATOR_AAL2_ONLY' using errcode = '42501'; end if;
      select * into r from app.deployments where id = new.rollback_of;
      if not found or r.env <> 'prod' or r.status <> 'deployed' or r.site_id <> new.site_id or r.customer_id <> new.customer_id
         or r.content_hash <> new.content_hash then
        raise exception 'ROLLBACK_MUST_RESTORE_A_DEPLOYED_APPROVED_ARTIFACT' using errcode = 'P0001';
      end if;
    end if;
  end if;
  return new;
end $$;
drop trigger prod_deploy_guard on app.deployments;
create trigger prod_deploy_guard before insert or update on app.deployments for each row execute function app.guard_prod_deploy();

-- ------------------------------------------------------------ audit chain: timezone-independent hash, explicit order, exportable head
alter table app.audit_log add column chain_seq bigint unique;
create or replace function app.audit_canonical(r app.audit_log, prev text) returns text
  language sql immutable set search_path = app, extensions, public, pg_temp as $$
  select encode(digest(prev || '|' || r.chain_seq || '|' || to_char(r.ts at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' ||
                       r.actor_type::text || '|' || r.actor_id || '|' || coalesce(r.customer_id::text, '') || '|' ||
                       r.action || '|' || coalesce(r.target, '') || '|' || r.details::text, 'sha256'), 'hex')
$$;
create or replace function app.audit_chain() returns trigger
language plpgsql security definer set search_path = app, extensions, public, pg_temp as $$
declare last app.audit_log;
begin
  perform pg_advisory_xact_lock(hashtext('app.audit_log'));
  select * into last from app.audit_log where chain_seq is not null order by chain_seq desc limit 1;
  new.chain_seq := coalesce(last.chain_seq, 0) + 1;       -- order assigned under the lock, not by the identity default
  new.prev_hash := coalesce(last.hash, 'GENESIS');
  new.hash := app.audit_canonical(new, new.prev_hash);
  return new;
end $$;
create or replace function app.audit_verify() returns table(broken_at bigint)
language plpgsql stable security definer set search_path = app, extensions, public, pg_temp as $$
declare r app.audit_log; prev text := 'GENESIS';
begin
  for r in select * from app.audit_log where chain_seq is not null order by chain_seq loop
    if r.prev_hash <> prev or r.hash <> app.audit_canonical(r, prev) then broken_at := r.chain_seq; return next; return; end if;
    prev := r.hash;
  end loop;
end $$;
-- Export the head daily to a store outside the database (docs/audit_checkpoints.md).
create or replace function app.audit_head() returns table(chain_seq bigint, hash text)
language sql stable security definer set search_path = app, pg_temp as $$
  select chain_seq, hash from app.audit_log where chain_seq is not null order by chain_seq desc limit 1
$$;
revoke all on function app.audit_verify(), app.audit_head() from public;

commit;
