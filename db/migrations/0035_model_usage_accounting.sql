-- =====================================================================
-- Hermes · 0035_model_usage_accounting.sql · spec 28.47, P29
-- Unknown usage keeps its reservation. Holds bind one model call to one
-- task/agent and immutable intent/prices; the existing budget functions
-- remain the only accounting path. No historic measurement is inferred.
-- =====================================================================
begin;

alter table app.agent_calls alter column tokens_in drop not null;
alter table app.agent_calls alter column tokens_out drop not null;
alter table app.agent_calls alter column cost_usd drop not null;
alter table app.agent_calls alter column tokens_in drop default;
alter table app.agent_calls alter column tokens_out drop default;
alter table app.agent_calls alter column cost_usd drop default;
alter table app.agent_calls add column usage_state text not null default 'unknown';
alter table app.agent_calls add column usage_source text not null default 'legacy.agent_calls';
alter table app.agent_calls add column usage_reason text default 'LEGACY_UNVERIFIED';
alter table app.agent_calls add constraint agent_calls_usage_shape check (
  usage_state in ('measured','estimated','unknown','invalid')
  and length(usage_source) between 1 and 120
  and (usage_reason is null or length(usage_reason) between 1 and 60)
  and (
    (usage_source = 'legacy.agent_calls' and usage_state = 'unknown' and usage_reason = 'LEGACY_UNVERIFIED')
    or (usage_state in ('measured','estimated') and tokens_in is not null and tokens_out is not null and cost_usd is not null)
    or (usage_state in ('unknown','invalid') and tokens_in is null and tokens_out is null and cost_usd is null)
  )
);
-- NOT VALID preserves any historic bad bytes as legacy/unverified while
-- enforcing finite costs for every new or updated row.
alter table app.agent_calls add constraint agent_calls_finite_cost check (
  cost_usd is null or cost_usd not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
) not valid;

create table app.model_usage_holds (
  call_key text primary key check (call_key ~ '^[0-9a-f]{64}$'),
  request_sha256 text not null check (request_sha256 ~ '^[0-9a-f]{64}$'),
  task_id uuid not null references app.tasks(id),
  customer_id uuid not null references app.customers(id),
  agent_id text not null,
  tool text not null,
  month_id text not null check (month_id ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
  model text not null,
  reserved_usd numeric(10,5) not null check (reserved_usd >= 0 and reserved_usd <> 'NaN'::numeric),
  monthly boolean not null,
  price_in numeric(18,12) not null check (price_in >= 0 and price_in <> 'NaN'::numeric),
  price_out numeric(18,12) not null check (price_out >= 0 and price_out <> 'NaN'::numeric),
  state text not null default 'reserved' check (state in ('reserved','pending_usage','settled','not_called')),
  usage_state text not null default 'unknown' check (usage_state in ('measured','estimated','unknown','invalid')),
  usage_source text not null default 'model.request' check (length(usage_source) between 1 and 120),
  usage_reason text default 'PENDING_RESPONSE' check (usage_reason is null or length(usage_reason) between 1 and 60),
  tokens_in int check (tokens_in >= 0),
  tokens_out int check (tokens_out >= 0),
  cost_usd numeric(10,5) check (cost_usd >= 0 and cost_usd <> 'NaN'::numeric),
  result jsonb,
  error_code text check (error_code is null or length(error_code) <= 60),
  evidence_sha256 text check (evidence_sha256 is null or evidence_sha256 ~ '^[0-9a-f]{64}$'),
  provider_ref text check (provider_ref is null or provider_ref ~ '^[A-Za-z0-9._:-]{1,128}$'),
  created_at timestamptz not null default clock_timestamp(),
  settled_at timestamptz,
  unique (task_id, agent_id),
  check (
    (usage_state in ('unknown','invalid') and tokens_in is null and tokens_out is null and cost_usd is null)
    or (usage_state in ('measured','estimated') and tokens_in is not null and tokens_out is not null and cost_usd is not null)
  ),
  check (state not in ('settled','not_called') or (settled_at is not null and usage_state in ('measured','estimated')))
);
create index model_usage_holds_customer on app.model_usage_holds (customer_id, created_at);
create index model_usage_holds_pending on app.model_usage_holds (created_at) where state in ('reserved','pending_usage');
alter table app.model_usage_holds enable row level security;
alter table app.model_usage_holds force row level security;
create policy model_usage_holds_worker on app.model_usage_holds for all to hermes_worker
  using (customer_id = (select app.worker_customer_id()) and task_id::text = current_setting('app.task_id', true))
  with check (customer_id = (select app.worker_customer_id()) and task_id::text = current_setting('app.task_id', true));
create policy model_usage_holds_operator_read on app.model_usage_holds for select to authenticated using ((select app.is_operator()));
grant select, insert, update on app.model_usage_holds to hermes_worker;
grant select on app.model_usage_holds to authenticated;

-- Following 0011/0020/0028: operators can only call the reconciliation
-- function. No authenticated user can UPDATE a hold or execute settlement
-- directly, including founder/tech. The definer's owner cannot log in,
-- bypass RLS, create schema objects or be assumed by an application role.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_usage_reconciler') then
    create role hermes_usage_reconciler nologin noinherit nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end $$;
grant usage on schema app to hermes_usage_reconciler;
grant select (auth_user_id,role,active) on app.operators to hermes_usage_reconciler;
create policy operators_usage_reconcile_read on app.operators for select to hermes_usage_reconciler
  using (auth_user_id = nullif(coalesce(nullif(current_setting('request.jwt.claim.sub',true),''),
    nullif(current_setting('request.jwt.claims',true),'')::jsonb->>'sub'),'')::uuid);
grant select on app.model_usage_holds to hermes_usage_reconciler;
grant update (state, usage_state, usage_source, usage_reason, tokens_in, tokens_out, cost_usd,
              evidence_sha256, provider_ref, settled_at) on app.model_usage_holds to hermes_usage_reconciler;
create policy model_usage_holds_reconciler_read on app.model_usage_holds for select to hermes_usage_reconciler
  using ((select app.is_operator()));
create policy model_usage_holds_reconciler_update on app.model_usage_holds for update to hermes_usage_reconciler
  using ((select app.is_operator())) with check ((select app.is_operator()));

grant select (customer_id,month_id,reserved_usd,spent_usd) on app.customer_ai_budget to hermes_usage_reconciler;
grant select (customer_id,agent_id,month_id,reserved_usd,spent_usd) on app.agent_state to hermes_usage_reconciler;
grant select (task_id,agent_id,reserved_usd,spent_usd) on app.task_budget to hermes_usage_reconciler;
grant update (reserved_usd,spent_usd) on app.customer_ai_budget, app.agent_state, app.task_budget to hermes_usage_reconciler;
create policy customer_ai_budget_usage_reconcile on app.customer_ai_budget for all to hermes_usage_reconciler
  using ((select app.is_operator()) and exists (select 1 from app.model_usage_holds h
    where h.customer_id = customer_ai_budget.customer_id and h.month_id = customer_ai_budget.month_id
      and h.state in ('reserved','pending_usage')))
  with check ((select app.is_operator()) and exists (select 1 from app.model_usage_holds h
    where h.customer_id = customer_ai_budget.customer_id and h.month_id = customer_ai_budget.month_id
      and h.state in ('reserved','pending_usage')));
create policy agent_state_usage_reconcile on app.agent_state for all to hermes_usage_reconciler
  using ((select app.is_operator()) and exists (select 1 from app.model_usage_holds h
    where h.customer_id = agent_state.customer_id and h.agent_id = agent_state.agent_id and h.month_id = agent_state.month_id
      and h.state in ('reserved','pending_usage')))
  with check ((select app.is_operator()) and exists (select 1 from app.model_usage_holds h
    where h.customer_id = agent_state.customer_id and h.agent_id = agent_state.agent_id and h.month_id = agent_state.month_id
      and h.state in ('reserved','pending_usage')));
create policy task_budget_usage_reconcile on app.task_budget for all to hermes_usage_reconciler
  using ((select app.is_operator()) and exists (select 1 from app.model_usage_holds h
    where h.task_id = task_budget.task_id and h.agent_id = task_budget.agent_id and h.state in ('reserved','pending_usage')))
  with check ((select app.is_operator()) and exists (select 1 from app.model_usage_holds h
    where h.task_id = task_budget.task_id and h.agent_id = task_budget.agent_id and h.state in ('reserved','pending_usage')));

grant select (agent_id,at) on app.reservation_exceed_events to hermes_usage_reconciler;
grant insert (agent_id,reserved_usd,actual_usd) on app.reservation_exceed_events to hermes_usage_reconciler;
create policy exceed_events_usage_reconcile on app.reservation_exceed_events for all to hermes_usage_reconciler
  using ((select app.is_operator())) with check ((select app.is_operator()));
grant select (agent_id) on app.agent_pauses to hermes_usage_reconciler;
grant insert (agent_id,reason) on app.agent_pauses to hermes_usage_reconciler;
create policy agent_pauses_usage_reconcile_read on app.agent_pauses for select to hermes_usage_reconciler
  using ((select app.is_operator()));
create policy agent_pauses_usage_reconcile_insert on app.agent_pauses for insert to hermes_usage_reconciler
  with check ((select app.is_operator()) and reason = 'RESERVATION_EXCEEDED' and released_at is null);
grant insert (actor_type,actor_id,customer_id,action,target,details) on app.audit_log to hermes_usage_reconciler;
create policy audit_usage_reconcile on app.audit_log for insert to hermes_usage_reconciler
  with check ((select app.is_operator()) and actor_type = 'operator'
              and actor_id = nullif(coalesce(nullif(current_setting('request.jwt.claim.sub',true),''),
                nullif(current_setting('request.jwt.claims',true),'')::jsonb->>'sub'),'')
              and action = 'model_usage.reconciled');

create function app.model_usage_hold_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if row(new.call_key,new.request_sha256,new.task_id,new.customer_id,new.agent_id,new.tool,new.month_id,
         new.model,new.reserved_usd,new.monthly,new.price_in,new.price_out,new.created_at)
     is distinct from
     row(old.call_key,old.request_sha256,old.task_id,old.customer_id,old.agent_id,old.tool,old.month_id,
         old.model,old.reserved_usd,old.monthly,old.price_in,old.price_out,old.created_at) then
    raise exception 'MODEL_USAGE_IMMUTABLE';
  end if;
  if old.state in ('settled','not_called') and new is distinct from old then
    raise exception 'MODEL_USAGE_FINAL';
  end if;
  if old.state = 'pending_usage' and new.state not in ('pending_usage','settled') then
    raise exception 'MODEL_USAGE_BAD_TRANSITION';
  end if;
  return new;
end $$;
revoke all on function app.model_usage_hold_guard() from public;
create trigger model_usage_hold_guard before update on app.model_usage_holds
  for each row execute function app.model_usage_hold_guard();

alter table app.agent_calls add column model_call_key text references app.model_usage_holds(call_key);
create unique index agent_calls_model_call_once on app.agent_calls(model_call_key) where model_call_key is not null;

-- Existing accounting authority, with a narrowly bound operator branch.
-- An operator cannot choose a customer or price: both come from the locked
-- hold, already populated with a measured receipt by reconcile_model_usage.
create or replace function app.settle_budget(p_agent text, p_month text, p_task uuid, p_reserved numeric, p_actual numeric, p_monthly boolean)
returns boolean language plpgsql security invoker set search_path = app, pg_temp as $$
declare cust uuid; h app.model_usage_holds; operator_path boolean := current_user = 'hermes_usage_reconciler'; u uuid;
begin
  if p_reserved is null or p_actual is null or p_monthly is null or p_reserved < 0 or p_actual < 0
     or p_reserved in ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)
     or p_actual in ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric) then
    raise exception 'MODEL_USAGE_INVALID';
  end if;
  if operator_path then
    if not app.is_operator() then raise exception 'OPERATOR_AAL2_ONLY' using errcode = '42501'; end if;
    u := nullif(coalesce(nullif(current_setting('request.jwt.claim.sub',true),''),
                 nullif(current_setting('request.jwt.claims',true),'')::jsonb->>'sub'),'')::uuid;
    if not exists (select 1 from app.operators where auth_user_id = u and active and role in ('founder','tech')) then
      raise exception 'MODEL_USAGE_TECH_ONLY' using errcode = '42501';
    end if;
    select * into h from app.model_usage_holds where task_id = p_task and agent_id = p_agent for update;
    if not found or h.month_id <> p_month or h.reserved_usd <> p_reserved or h.monthly <> p_monthly
       or h.usage_state <> 'measured' or h.usage_source <> 'operator.provider_receipt'
       or h.evidence_sha256 is null or h.provider_ref is null
       or h.cost_usd is null or h.cost_usd <> p_actual
       or ceil((h.tokens_in * h.price_in + h.tokens_out * h.price_out) * 100000) / 100000 <> p_actual then
      raise exception 'MODEL_USAGE_NOT_READY';
    end if;
    if h.state = 'settled' then return false; end if;
    if h.state not in ('reserved','pending_usage') then raise exception 'MODEL_USAGE_FINAL'; end if;
    cust := h.customer_id;
  else
    -- Preserve the existing tenant-scoped settlement contract. New model
    -- holds are additionally scoped to the exact leased task by their RLS.
    if current_user <> 'hermes_worker' or coalesce(app.worker_context(),'none') = 'none' then
      raise exception 'LEASE_INVALID' using errcode = '42501';
    end if;
    cust := app.worker_customer_id();
  end if;

  -- Preserve the existing lock order. Under-reserved/missing rows refuse the
  -- whole transaction; clamping a missing reservation to zero hid mismatches.
  if p_monthly then
    update app.customer_ai_budget set reserved_usd = reserved_usd - p_reserved, spent_usd = spent_usd + p_actual
      where customer_id = cust and month_id = p_month and reserved_usd >= p_reserved;
    if not found then raise exception 'MODEL_BUDGET_INCONSISTENT'; end if;
  end if;
  if cust is not null then
    update app.agent_state set reserved_usd = reserved_usd - p_reserved, spent_usd = spent_usd + p_actual
      where customer_id = cust and agent_id = p_agent and month_id = p_month and reserved_usd >= p_reserved;
  else
    update app.acquisition_budget set reserved_usd = reserved_usd - p_reserved, spent_usd = spent_usd + p_actual
      where month_id = p_month and reserved_usd >= p_reserved;
  end if;
  if not found then raise exception 'MODEL_BUDGET_INCONSISTENT'; end if;
  update app.task_budget set reserved_usd = reserved_usd - p_reserved, spent_usd = spent_usd + p_actual
    where task_id = p_task and agent_id = p_agent and reserved_usd >= p_reserved;
  if not found then raise exception 'MODEL_BUDGET_INCONSISTENT'; end if;
  if operator_path then
    update app.model_usage_holds set state = 'settled', settled_at = clock_timestamp() where call_key = h.call_key;
  end if;
  return p_actual > p_reserved;
end $$;
revoke all on function app.settle_budget(text,text,uuid,numeric,numeric,boolean) from public;
grant execute on function app.settle_budget(text,text,uuid,numeric,numeric,boolean),
  app.record_reservation_exceeded(text,numeric,numeric) to hermes_usage_reconciler;

create function app.reconcile_model_usage(p_key text, p_tokens_in int, p_tokens_out int,
                                        p_evidence_sha256 text, p_provider_ref text, p_reason text)
returns jsonb language plpgsql security definer set search_path = app, pg_temp as $$
declare h app.model_usage_holds; actual numeric; exceeded boolean; u uuid;
begin
  if not app.is_operator() then raise exception 'OPERATOR_AAL2_ONLY' using errcode = '42501'; end if;
  -- Same verified-session identity extraction as link_provider_account (0028),
  -- without granting the private role access to the authentication schema.
  u := nullif(coalesce(nullif(current_setting('request.jwt.claim.sub',true),''),
               nullif(current_setting('request.jwt.claims',true),'')::jsonb->>'sub'),'')::uuid;
  if not exists (select 1 from app.operators where auth_user_id = u and active and role in ('founder','tech')) then
    raise exception 'MODEL_USAGE_TECH_ONLY' using errcode = '42501';
  end if;
  if p_reason is null or length(trim(p_reason)) < 5 then raise exception 'REASON_REQUIRED'; end if;
  if p_tokens_in is null or p_tokens_out is null or p_tokens_in < 0 or p_tokens_out < 0
     or p_evidence_sha256 is null or p_evidence_sha256 !~ '^[0-9a-f]{64}$'
     or p_provider_ref is null or p_provider_ref !~ '^[A-Za-z0-9._:-]{1,128}$' then
    raise exception 'MODEL_USAGE_INVALID';
  end if;
  select * into h from app.model_usage_holds where call_key = p_key for update;
  if not found then raise exception 'MODEL_CALL_NOT_FOUND'; end if;
  if h.state = 'settled' then
    if h.usage_source = 'operator.provider_receipt' and h.tokens_in = p_tokens_in and h.tokens_out = p_tokens_out
       and h.evidence_sha256 = p_evidence_sha256 and h.provider_ref = p_provider_ref then
      return jsonb_build_object('call_key',p_key,'state','settled','changed',false,'cost_usd',h.cost_usd,'customer_id',h.customer_id);
    end if;
    raise exception 'MODEL_RECONCILIATION_CONFLICT';
  end if;
  if h.state not in ('reserved','pending_usage') then raise exception 'MODEL_USAGE_FINAL'; end if;
  actual := ceil((p_tokens_in * h.price_in + p_tokens_out * h.price_out) * 100000) / 100000;
  if actual > 99999.99999 then raise exception 'MODEL_USAGE_INVALID'; end if;
  update app.model_usage_holds set usage_state = 'measured', usage_source = 'operator.provider_receipt',
    usage_reason = 'RECONCILED_OPERATOR_EVIDENCE', tokens_in = p_tokens_in, tokens_out = p_tokens_out,
    cost_usd = actual, evidence_sha256 = p_evidence_sha256, provider_ref = p_provider_ref where call_key = p_key;
  exceeded := app.settle_budget(h.agent_id,h.month_id,h.task_id,h.reserved_usd,actual,h.monthly);
  if exceeded then perform app.record_reservation_exceeded(h.agent_id,h.reserved_usd,actual); end if;
  insert into app.audit_log(actor_type,actor_id,customer_id,action,target,details)
    values ('operator',u::text,h.customer_id,'model_usage.reconciled',p_key,
      jsonb_build_object('reason',p_reason,'from',h.state,'reserved_usd',h.reserved_usd,'cost_usd',actual,
        'evidence_sha256',p_evidence_sha256,'provider_ref',p_provider_ref,
        'usage_source','operator.provider_receipt','evidence_authenticity','operator_asserted_not_cryptographically_attested'));
  return jsonb_build_object('call_key',p_key,'state','settled','changed',true,'cost_usd',actual,'customer_id',h.customer_id);
end $$;
revoke all on function app.reconcile_model_usage(text,int,int,text,text,text) from public;
grant execute on function app.reconcile_model_usage(text,int,int,text,text,text) to authenticated;
grant hermes_usage_reconciler to current_user;
grant create on schema app to hermes_usage_reconciler;
alter function app.reconcile_model_usage(text,int,int,text,text,text) owner to hermes_usage_reconciler;
revoke create on schema app from hermes_usage_reconciler;
do $$ begin execute format('revoke hermes_usage_reconciler from %I', current_user); end $$;

-- Observations are immutable. This view exposes a subsequent reconciliation
-- beside its original state, with the caller's RLS on both underlying tables.
create view app.v_agent_calls_effective with (security_invoker = true) as
select c.id,c.ts,c.task_id,c.customer_id,c.agent_id,c.tool,c.model,
  case when h.state = 'settled' then h.tokens_in else c.tokens_in end as tokens_in,
  case when h.state = 'settled' then h.tokens_out else c.tokens_out end as tokens_out,
  case when h.state = 'settled' then h.cost_usd else c.cost_usd end as cost_usd,
  c.duration_ms,c.outcome,c.error_code,c.idempotency_key,
  case when h.state = 'settled' then h.usage_state else c.usage_state end as usage_state,
  case when h.state = 'settled' then h.usage_source else c.usage_source end as usage_source,
  case when h.state = 'settled' then h.usage_reason else c.usage_reason end as usage_reason,
  c.usage_state as observed_usage_state,c.model_call_key,h.evidence_sha256,h.provider_ref,true as observation_present
from app.agent_calls c left join app.model_usage_holds h on h.call_key = c.model_call_key
union all
-- A lost worker response may leave only the durable reservation. Include
-- that call once in reports without inventing an observed answer or time.
select null::bigint,h.created_at,h.task_id,h.customer_id,h.agent_id,h.tool,h.model,
  h.tokens_in,h.tokens_out,h.cost_usd,null::int,'failure'::app.call_outcome,
  coalesce(h.error_code,'MODEL_RESPONSE_UNOBSERVED'),null::text,h.usage_state,h.usage_source,h.usage_reason,
  null::text,h.call_key,h.evidence_sha256,h.provider_ref,false
from app.model_usage_holds h where not exists (select 1 from app.agent_calls c where c.model_call_key = h.call_key);
grant select on app.v_agent_calls_effective to authenticated, hermes_worker;
create or replace view app.v_ai_cost_30d with (security_invoker = true) as
select c.customer_id,sum(c.cost_usd) as ai_cost_usd_30d,sum(c.tokens_in)::bigint as tokens_in_30d,
  sum(c.tokens_out)::bigint as tokens_out_30d,count(*) filter (where c.outcome <> 'success') as non_success_calls,
  count(*) filter (where c.usage_state <> 'measured') as unverified_calls
from app.v_agent_calls_effective c where c.customer_id is not null and c.ts > now() - interval '30 days' group by c.customer_id;
create or replace view app.v_agent_monthly_report with (security_invoker = true) as
select c.agent_id,date_trunc('month',c.ts)::date as month,count(*) as calls,
  round(avg((c.outcome = 'success')::int)::numeric,4) as success_rate,sum(c.cost_usd) as cost_usd,
  percentile_cont(0.95) within group (order by c.duration_ms) as p95_ms,
  count(*) filter (where c.usage_state <> 'measured') as unverified_calls
from app.v_agent_calls_effective c group by 1,2;

grant execute on function app.is_operator() to hermes_usage_reconciler;

commit;
