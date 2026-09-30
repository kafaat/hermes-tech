-- =====================================================================
-- Hermes · 0008_v16_leases_terminal_pause.sql · review of 1.5
--   N1 probe lease: a probe whose worker died expires (timeout + 60 s, below the cooldown)
--   N2 key lease: an in-flight key whose worker died can be taken over after its lease
--   N3 terminal failures become 'rejected' and are never re-claimed without an audited release
--   N5 a second RESERVATION_EXCEEDED for an agent within 24 h pauses that agent
--   IN_PROGRESS protocol: the caller re-queues its task with run_after (docs/delivery_protocol.md)
-- Global lock order (docs/lock_order.md):
--   tasks -> acquisition_budget -> customer_ai_budget -> agent_state -> task_budget
--   idempotency_keys and agent_pauses are touched in their own short transactions.
-- =====================================================================
begin;

-- ------------------------------------------------------------ re-queue with delay
alter table app.tasks add column run_after timestamptz not null default now();

create or replace function app.claim_task(p_agent_id text)
returns app.tasks language plpgsql security invoker set search_path = app, pg_temp as $$
declare t app.tasks;
begin
  select * into t from app.tasks
   where status = 'queued' and agent_id = p_agent_id and run_after <= now()
   order by priority desc, created_at
   for update skip locked
   limit 1;
  if found then
    update app.tasks set status = 'running', started_at = now(), attempts = attempts + 1
     where id = t.id returning * into t;
  end if;
  return t;
end $$;

-- A duplicate that got IN_PROGRESS neither succeeds nor fails: it goes back to the queue.
create or replace function app.requeue_task(p_task uuid, p_delay_seconds int)
returns void language sql security invoker set search_path = app, pg_temp as $$
  update app.tasks set status = 'queued', run_after = now() + make_interval(secs => greatest(p_delay_seconds, 1))
  where id = p_task and status = 'running';
$$;

-- ------------------------------------------------------------ N2/N3 idempotency leases and terminal state
alter type app.idem_status add value if not exists 'rejected';
alter table app.idempotency_keys add column lease_until timestamptz;

drop function app.claim_idempotency(text, text);
create function app.claim_idempotency(p_key text, p_agent text, p_lease_seconds int default 900)
returns jsonb language plpgsql security invoker set search_path = app, pg_temp as $$
declare r app.idempotency_keys;
begin
  insert into app.idempotency_keys (key, customer_id, agent_id, lease_until)
  values (p_key, app.worker_customer_id(), p_agent, now() + make_interval(secs => p_lease_seconds))
  on conflict (key) do update
     set status = 'in_progress', claimed_at = now(), completed_at = null, result = null,
         lease_until = now() + make_interval(secs => p_lease_seconds)
   where app.idempotency_keys.status = 'failed'                                    -- transient failure
      or (app.idempotency_keys.status = 'in_progress' and app.idempotency_keys.lease_until < now());  -- dead holder
  if found then return null; end if;
  select * into r from app.idempotency_keys where key = p_key;
  if r.status = 'succeeded' then return r.result; end if;
  if r.status::text = 'rejected' then return jsonb_build_object('status', 'FAILED', 'terminal', true); end if;
  return jsonb_build_object('status', 'IN_PROGRESS',
                            'retry_after', least(30, greatest(1, extract(epoch from r.lease_until - now())::int)));
end $$;

-- Transient failures release the key; terminal ones mark it rejected.
create or replace function app.fail_idempotency(p_key text, p_terminal boolean)
returns void language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if p_terminal then
    update app.idempotency_keys set status = 'rejected', completed_at = now() where key = p_key and status = 'in_progress';
  else
    update app.idempotency_keys set status = 'failed', completed_at = now() where key = p_key and status = 'in_progress';
  end if;
end $$;

-- The only way out of 'rejected': an operator decision with a reason, written to the audit log.
create or replace function app.operator_release_rejected(p_key text, p_reason text)
returns void language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if not (select app.is_operator()) then raise exception 'OPERATOR_ONLY' using errcode = '42501'; end if;
  if coalesce(length(trim(p_reason)), 0) < 5 then raise exception 'REASON_REQUIRED' using errcode = 'P0001'; end if;
  update app.idempotency_keys set status = 'failed' where key = p_key and status::text = 'rejected';
  insert into app.audit_log (actor_type, actor_id, action, target, details)
  values ('operator', coalesce(auth.uid()::text, 'unknown'), 'idempotency.released', p_key, jsonb_build_object('reason', p_reason));
end $$;

-- ------------------------------------------------------------ outbox dispatch lease (docs/delivery_protocol.md)
alter table app.outbox add column sending_until timestamptz;
alter table app.outbox add column provider_message_id text;
alter table app.outbox add column needs_human_check boolean not null default false;
-- A row whose lease expired while sending is ambiguous (sent or not?). It is reconciled from the
-- provider's status webhooks by provider_message_id, otherwise flagged for a human; never auto-resent.
create or replace function app.flag_ambiguous_outbox()
returns int language sql security invoker set search_path = app, pg_temp as $$
  with f as (
    update app.outbox set needs_human_check = true
     where dispatched_at is null and sending_until is not null and sending_until < now() - interval '10 minutes'
       and not needs_human_check
    returning 1)
  select count(*)::int from f;
$$;

-- ------------------------------------------------------------ N1 probe lease
alter table app.agent_state add column probe_until timestamptz;

drop function app.claim_probe(text, text, int);
create function app.claim_probe(p_agent text, p_month text, p_cooldown_seconds int, p_probe_lease_seconds int default 660)
returns text language plpgsql security invoker set search_path = app, pg_temp as $$
declare ag app.agent_state;
begin
  if p_probe_lease_seconds >= p_cooldown_seconds then raise exception 'PROBE_LEASE_MUST_BE_BELOW_COOLDOWN'; end if;
  select * into ag from app.agent_state
   where customer_id = app.worker_customer_id() and agent_id = p_agent and month_id = p_month for update;
  if not found or ag.circuit = 'closed' then return 'CLOSED'; end if;
  if ag.circuit_opened_at is null or now() - ag.circuit_opened_at < make_interval(secs => p_cooldown_seconds) then
    return 'OPEN';
  end if;
  if ag.probe_in_flight and ag.probe_until is not null and ag.probe_until > now() then
    return 'OPEN';                                      -- a live probe exists
  end if;
  update app.agent_state set circuit = 'half_open', probe_in_flight = true,
         probe_until = now() + make_interval(secs => p_probe_lease_seconds)
   where customer_id = ag.customer_id and agent_id = p_agent and month_id = p_month;
  return 'PROBE';
end $$;

-- ------------------------------------------------------------ N5 pause on repeated overshoot
create table app.reservation_exceed_events (           -- no tenant data: agent, amounts, time
  id              bigint generated always as identity primary key,
  agent_id        text not null,
  reserved_usd    numeric(10,5) not null,
  actual_usd      numeric(10,5) not null check (actual_usd > reserved_usd),
  at              timestamptz not null default now()
);
create table app.agent_pauses (
  agent_id        text primary key,
  reason          text not null check (reason in ('RESERVATION_EXCEEDED','OPERATOR')),
  paused_at       timestamptz not null default now(),
  released_by     uuid,
  released_at     timestamptz,
  release_reason  text,
  check (released_at is null or (released_by is not null and length(trim(release_reason)) >= 5))
);

alter table app.reservation_exceed_events enable row level security;
alter table app.reservation_exceed_events force row level security;
create policy exceed_events_operator_all on app.reservation_exceed_events for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy exceed_events_worker_rw on app.reservation_exceed_events for all to hermes_worker using (true) with check (true);
grant select, insert on app.reservation_exceed_events to hermes_worker, authenticated;

alter table app.agent_pauses enable row level security;
alter table app.agent_pauses force row level security;
create policy agent_pauses_operator_all on app.agent_pauses for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy agent_pauses_worker_read on app.agent_pauses for select to hermes_worker using (true);
create policy agent_pauses_worker_pause on app.agent_pauses for insert to hermes_worker with check (reason = 'RESERVATION_EXCEEDED' and released_at is null);
grant select, insert on app.agent_pauses to hermes_worker;
grant select, insert, update on app.agent_pauses to authenticated;

-- Called by the worker after settle_budget returned true.
create or replace function app.record_reservation_exceeded(p_agent text, p_reserved numeric, p_actual numeric)
returns boolean language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  insert into app.reservation_exceed_events (agent_id, reserved_usd, actual_usd) values (p_agent, p_reserved, p_actual);
  if (select count(*) from app.reservation_exceed_events where agent_id = p_agent and at > now() - interval '24 hours') >= 2 then
    insert into app.agent_pauses (agent_id, reason) values (p_agent, 'RESERVATION_EXCEEDED')
    on conflict (agent_id) do nothing;
    return true;                                        -- paused
  end if;
  return false;
end $$;

-- reserve_budget refuses a paused agent before touching any budget row.
create or replace function app.agent_is_paused(p_agent text)
returns boolean language sql stable security invoker set search_path = app, pg_temp as $$
  select exists (select 1 from app.agent_pauses where agent_id = p_agent and released_at is null)
$$;

-- reserve_budget and record_outcome redefined: pause check first; probe lease cleared on every outcome
create or replace function app.reserve_budget(p_agent text, p_month text, p_task uuid, p_amount numeric,
                                              p_per_call numeric, p_per_task numeric, p_agent_cap numeric, p_customer_cap numeric)
returns text language plpgsql security invoker set search_path = app, pg_temp as $$
declare cust uuid := app.worker_customer_id(); cb app.customer_ai_budget; ag app.agent_state; tb app.task_budget;
        ab app.acquisition_budget;
begin
  if app.agent_is_paused(p_agent) then return 'AGENT_PAUSED'; end if;           -- N5, before any lock
  if p_amount > p_per_call then return 'BUDGET_EXCEEDED_CALL'; end if;
  if cust is null then                                          -- acquisition: agent cap = global monthly pool
    insert into app.acquisition_budget (month_id) values (p_month) on conflict do nothing;
    select * into ab from app.acquisition_budget where month_id = p_month for update;
  end if;
  if p_customer_cap is not null then
    insert into app.customer_ai_budget (customer_id, month_id) values (cust, p_month) on conflict do nothing;
    select * into cb from app.customer_ai_budget where customer_id = cust and month_id = p_month for update;
  end if;
  if cust is not null then
    insert into app.agent_state (customer_id, agent_id, month_id) values (cust, p_agent, p_month) on conflict do nothing;
    select * into ag from app.agent_state where customer_id = cust and agent_id = p_agent and month_id = p_month for update;
  end if;
  insert into app.task_budget (task_id, agent_id, customer_id) values (p_task, p_agent, cust) on conflict do nothing;
  select * into tb from app.task_budget where task_id = p_task and agent_id = p_agent for update;

  if tb.spent_usd + tb.reserved_usd + p_amount > p_per_task then return 'BUDGET_EXCEEDED_TASK'; end if;
  if cust is not null and ag.spent_usd + ag.reserved_usd + p_amount > p_agent_cap then return 'BUDGET_EXCEEDED_AGENT'; end if;
  if cust is null and ab.spent_usd + ab.reserved_usd + p_amount > p_agent_cap then return 'BUDGET_EXCEEDED_AGENT'; end if;
  if p_customer_cap is not null and cb.spent_usd + cb.reserved_usd + p_amount > p_customer_cap then return 'BUDGET_EXCEEDED_CUSTOMER'; end if;

  if p_customer_cap is not null then
    update app.customer_ai_budget set reserved_usd = reserved_usd + p_amount where customer_id = cust and month_id = p_month;
  end if;
  if cust is not null then
    update app.agent_state set reserved_usd = reserved_usd + p_amount where customer_id = cust and agent_id = p_agent and month_id = p_month;
  else
    update app.acquisition_budget set reserved_usd = reserved_usd + p_amount where month_id = p_month;
  end if;
  update app.task_budget set reserved_usd = reserved_usd + p_amount where task_id = p_task and agent_id = p_agent;
  return 'OK';
end $$;

create or replace function app.record_outcome(p_agent text, p_month text, p_success boolean, p_was_probe boolean, p_threshold int)
returns app.circuit_state language plpgsql security invoker set search_path = app, pg_temp as $$
declare ag app.agent_state;
begin
  select * into ag from app.agent_state
   where customer_id = app.worker_customer_id() and agent_id = p_agent and month_id = p_month for update;
  if not found then return 'closed'; end if;
  if p_success then
    update app.agent_state set consecutive_failures = 0, circuit = 'closed', circuit_opened_at = null, probe_in_flight = false, probe_until = null, calls = calls + 1
     where customer_id = ag.customer_id and agent_id = p_agent and month_id = p_month;
    return 'closed';
  end if;
  update app.agent_state
     set consecutive_failures = consecutive_failures + 1, probe_in_flight = false, probe_until = null,
         circuit = case when p_was_probe or consecutive_failures + 1 >= p_threshold then 'open'::app.circuit_state else circuit end,
         circuit_opened_at = case when p_was_probe or consecutive_failures + 1 >= p_threshold then now() else circuit_opened_at end
   where customer_id = ag.customer_id and agent_id = p_agent and month_id = p_month
  returning circuit into ag.circuit;
  return ag.circuit;
end $$;

grant execute on function app.claim_idempotency(text, text, int) to hermes_worker;
grant execute on function app.requeue_task(uuid, int), app.fail_idempotency(text, boolean),
  app.claim_probe(text, text, int, int), app.record_reservation_exceeded(text, numeric, numeric), app.agent_is_paused(text) to hermes_worker;
grant execute on function app.operator_release_rejected(text, text) to authenticated;
grant execute on function app.flag_ambiguous_outbox() to hermes_worker;

commit;
