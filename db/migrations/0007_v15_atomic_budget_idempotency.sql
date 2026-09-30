-- =====================================================================
-- Hermes · 0007_v15_atomic_budget_idempotency.sql
-- Production counterpart of the v1.5 fixes in tools/enforce.py:
--   R1  budget check and reservation are ONE step under row locks (no overshoot)
--   R2  an idempotency key is claimed before execution (no double execution)
--   R3  keys are scoped agent|action|tool|fields (enforced by format check)
--   R5  a half-open circuit admits exactly one probe
--   R6  per-task spend is tracked and capped
-- Lock order everywhere: customer_ai_budget -> agent_state -> task_budget (no deadlocks).
-- All functions are SECURITY INVOKER: they run inside the worker's tenant scope (RLS).
-- =====================================================================
begin;

alter table app.agent_state add column reserved_usd numeric(10,5) not null default 0 check (reserved_usd >= 0);
alter table app.agent_state add column probe_in_flight boolean not null default false;

create table app.customer_ai_budget (
  customer_id     uuid not null references app.customers(id),
  month_id        text not null check (month_id ~ '^\d{4}-\d{2}$'),
  spent_usd       numeric(10,5) not null default 0 check (spent_usd >= 0),
  reserved_usd    numeric(10,5) not null default 0 check (reserved_usd >= 0),
  primary key (customer_id, month_id)
);

create table app.task_budget (
  task_id         uuid not null references app.tasks(id),
  agent_id        text not null,
  customer_id     uuid references app.customers(id),      -- null = acquisition context
  spent_usd       numeric(10,5) not null default 0 check (spent_usd >= 0),
  reserved_usd    numeric(10,5) not null default 0 check (reserved_usd >= 0),
  primary key (task_id, agent_id)
);

create table app.acquisition_budget (                        -- global monthly pool of agent_search (no tenant)
  month_id        text primary key check (month_id ~ '^\d{4}-\d{2}$'),
  spent_usd       numeric(10,5) not null default 0 check (spent_usd >= 0),
  reserved_usd    numeric(10,5) not null default 0 check (reserved_usd >= 0)
);

create type app.idem_status as enum ('in_progress','succeeded','failed');
create table app.idempotency_keys (
  key             text primary key check (key ~ '^agent_[a-z0-9_]+\|[a-z_]+:[a-z_*]+\|[a-z_]+\.[a-z_]+\|.+$'),
  customer_id     uuid references app.customers(id),
  agent_id        text not null,
  status          app.idem_status not null default 'in_progress',
  result          jsonb,
  claimed_at      timestamptz not null default now(),
  completed_at    timestamptz,
  check (status <> 'succeeded' or (result is not null and completed_at is not null))
);

-- ------------------------------------------------------------ RLS
alter table app.customer_ai_budget enable row level security;
alter table app.customer_ai_budget force row level security;
create policy customer_ai_budget_operator_all on app.customer_ai_budget for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy customer_ai_budget_worker_rw on app.customer_ai_budget for all to hermes_worker using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select, insert, update on app.customer_ai_budget to hermes_worker, authenticated;

alter table app.task_budget enable row level security;
alter table app.task_budget force row level security;
create policy task_budget_operator_all on app.task_budget for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy task_budget_worker_rw on app.task_budget for all to hermes_worker
  using (customer_id is not distinct from (select app.worker_customer_id()))
  with check (customer_id is not distinct from (select app.worker_customer_id()));
grant select, insert, update on app.task_budget to hermes_worker, authenticated;

alter table app.acquisition_budget enable row level security;
alter table app.acquisition_budget force row level security;
create policy acquisition_budget_operator_all on app.acquisition_budget for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy acquisition_budget_worker_rw on app.acquisition_budget for all to hermes_worker
  using ((select app.worker_customer_id()) is null) with check ((select app.worker_customer_id()) is null);
grant select, insert, update on app.acquisition_budget to hermes_worker, authenticated;

alter table app.idempotency_keys enable row level security;
alter table app.idempotency_keys force row level security;
create policy idempotency_keys_operator_all on app.idempotency_keys for all to authenticated using ((select app.is_operator())) with check ((select app.is_operator()));
create policy idempotency_keys_worker_rw on app.idempotency_keys for all to hermes_worker
  using (customer_id is not distinct from (select app.worker_customer_id()))
  with check (customer_id is not distinct from (select app.worker_customer_id()));
grant select, insert, update on app.idempotency_keys to hermes_worker;   -- no delete: failed keys are re-claimed, not removed
grant select on app.idempotency_keys to authenticated;

-- ------------------------------------------------------------ R2/R3 idempotency claim
-- Returns NULL when the caller owns the key and must execute; otherwise the stored
-- result, or {"status":"IN_PROGRESS"} while another worker is executing it.
-- Call it in its own short transaction and COMMIT before the external call, so a
-- concurrent duplicate sees the claim instead of waiting on an open transaction.
create or replace function app.claim_idempotency(p_key text, p_agent text)
returns jsonb language plpgsql security invoker set search_path = app, pg_temp as $$
declare r app.idempotency_keys;
begin
  insert into app.idempotency_keys (key, customer_id, agent_id)
  values (p_key, app.worker_customer_id(), p_agent)
  on conflict (key) do update set status = 'in_progress', claimed_at = now(), completed_at = null, result = null
    where app.idempotency_keys.status = 'failed';               -- only a failed key can be claimed again
  if found then return null; end if;
  select * into r from app.idempotency_keys where key = p_key;
  if r.status = 'succeeded' then return r.result; end if;
  return '{"status":"IN_PROGRESS"}'::jsonb;
end $$;

create or replace function app.complete_idempotency(p_key text, p_result jsonb)
returns void language sql security invoker set search_path = app, pg_temp as $$
  update app.idempotency_keys set status = 'succeeded', result = p_result, completed_at = now()
  where key = p_key and status = 'in_progress';
$$;

create or replace function app.release_idempotency(p_key text)   -- failed execution: key may be claimed again
returns void language sql security invoker set search_path = app, pg_temp as $$
  update app.idempotency_keys set status = 'failed', completed_at = now() where key = p_key and status = 'in_progress';
$$;

-- ------------------------------------------------------------ R1/R6 atomic reservation
-- Returns 'OK' or the error code. p_customer_cap is NULL for non-monthly budget classes.
create or replace function app.reserve_budget(p_agent text, p_month text, p_task uuid, p_amount numeric,
                                              p_per_call numeric, p_per_task numeric, p_agent_cap numeric, p_customer_cap numeric)
returns text language plpgsql security invoker set search_path = app, pg_temp as $$
declare cust uuid := app.worker_customer_id(); cb app.customer_ai_budget; ag app.agent_state; tb app.task_budget;
        ab app.acquisition_budget;
begin
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

-- Replace a reservation by the actual cost. Returns true when actual > reserved (RESERVATION_EXCEEDED).
create or replace function app.settle_budget(p_agent text, p_month text, p_task uuid, p_reserved numeric, p_actual numeric, p_monthly boolean)
returns boolean language plpgsql security invoker set search_path = app, pg_temp as $$
declare cust uuid := app.worker_customer_id();
begin
  if p_monthly then
    update app.customer_ai_budget set reserved_usd = greatest(reserved_usd - p_reserved, 0), spent_usd = spent_usd + p_actual
    where customer_id = cust and month_id = p_month;
  end if;
  if cust is not null then
    update app.agent_state set reserved_usd = greatest(reserved_usd - p_reserved, 0), spent_usd = spent_usd + p_actual
    where customer_id = cust and agent_id = p_agent and month_id = p_month;
  else
    update app.acquisition_budget set reserved_usd = greatest(reserved_usd - p_reserved, 0), spent_usd = spent_usd + p_actual
    where month_id = p_month;
  end if;
  update app.task_budget set reserved_usd = greatest(reserved_usd - p_reserved, 0), spent_usd = spent_usd + p_actual
  where task_id = p_task and agent_id = p_agent;
  return p_actual > p_reserved;
end $$;

-- ------------------------------------------------------------ R5 single probe
-- Returns 'CLOSED' (proceed), 'PROBE' (this caller is the one probe) or 'OPEN' (refuse).
create or replace function app.claim_probe(p_agent text, p_month text, p_cooldown_seconds int)
returns text language plpgsql security invoker set search_path = app, pg_temp as $$
declare ag app.agent_state;
begin
  select * into ag from app.agent_state
   where customer_id = app.worker_customer_id() and agent_id = p_agent and month_id = p_month for update;
  if not found or ag.circuit = 'closed' then return 'CLOSED'; end if;
  if ag.probe_in_flight or ag.circuit_opened_at is null
     or now() - ag.circuit_opened_at < make_interval(secs => p_cooldown_seconds) then
    return 'OPEN';
  end if;
  update app.agent_state set circuit = 'half_open', probe_in_flight = true
   where customer_id = ag.customer_id and agent_id = p_agent and month_id = p_month;
  return 'PROBE';
end $$;

-- Record the outcome of a call: resets or trips the circuit and always releases the probe.
create or replace function app.record_outcome(p_agent text, p_month text, p_success boolean, p_was_probe boolean, p_threshold int)
returns app.circuit_state language plpgsql security invoker set search_path = app, pg_temp as $$
declare ag app.agent_state;
begin
  select * into ag from app.agent_state
   where customer_id = app.worker_customer_id() and agent_id = p_agent and month_id = p_month for update;
  if not found then return 'closed'; end if;
  if p_success then
    update app.agent_state set consecutive_failures = 0, circuit = 'closed', circuit_opened_at = null, probe_in_flight = false, calls = calls + 1
     where customer_id = ag.customer_id and agent_id = p_agent and month_id = p_month;
    return 'closed';
  end if;
  update app.agent_state
     set consecutive_failures = consecutive_failures + 1, probe_in_flight = false,
         circuit = case when p_was_probe or consecutive_failures + 1 >= p_threshold then 'open'::app.circuit_state else circuit end,
         circuit_opened_at = case when p_was_probe or consecutive_failures + 1 >= p_threshold then now() else circuit_opened_at end
   where customer_id = ag.customer_id and agent_id = p_agent and month_id = p_month
  returning circuit into ag.circuit;
  return ag.circuit;
end $$;

grant execute on function app.record_outcome(text, text, boolean, boolean, int) to hermes_worker;
grant execute on function app.claim_idempotency(text, text), app.complete_idempotency(text, jsonb), app.release_idempotency(text),
  app.reserve_budget(text, text, uuid, numeric, numeric, numeric, numeric, numeric),
  app.settle_budget(text, text, uuid, numeric, numeric, boolean), app.claim_probe(text, text, int) to hermes_worker;

commit;
