-- =====================================================================
-- Hermes · 0010_v18_closure.sql · review of 1.7 (authority and closure evidence)
-- Defects found by reading 0009 against the final catalog it produces (docs/review_response_v17.md):
--   L1 lease checks used now() = TRANSACTION start: a transaction that began before its lease
--      expired kept the tenant until it ended. Leases are now checked against clock_timestamp().
--   L2 complete_task accepted an EXPIRED lease whose token recovery had not rotated, and did not
--      require a running task; recovery now rotates the token, completion needs a live lease.
--   L3 extend_task_lease had no ceiling (a hung worker could hold a task for years): max 900 s.
--   T1 the worker held table-level UPDATE on app.tasks: it could reset attempts (dodge the dead
--      letter) or re-queue a task another worker holds. Tasks now change only through lease functions.
--   O1 claim_outbox_dispatch re-claimed a row as soon as its sending lease expired, while
--      flag_ambiguous_outbox waited 10 minutes: an unconfirmed WhatsApp reply could be re-sent.
--   O2 the worker held table-level UPDATE on app.outbox: it could clear needs_human_check.
--   O3 a redelivered request for an already-consumed approval failed instead of returning its row.
--      -> outbox dispatch is a state machine in the database (below).
--   W1 0009 said "a definer function routes" webhook events; none existed, so no event could ever
--      reach a tenant. Routing now happens at ingest, from channel_accounts, never from the payload
--      sender. The worker may set processed_at only (it could rewrite signature_valid before).
--   A1 audit rows could impersonate: an owner could insert actor_type 'operator'/'system' with any
--      actor_id; an unleased worker could write platform rows. Effects are audited by the database
--      itself, inside the effect's transaction (review A15, first half).
--   C1 approval consumption was guarded by a session setting any role can set; it is now guarded
--      by the executing role being the table owner (only the outbox trigger runs as owner).
--   R1 inquiry bodies are purged after 30 days by a job role with column-limited rights (C4.7).
-- =====================================================================
begin;

-- ------------------------------------------------------------ L1–L3 leases against the wall clock
create or replace function app.worker_customer_id() returns uuid
  language sql stable security definer set search_path = app, pg_temp as $$
  select l.customer_id from app.task_leases l
   where l.task_id = nullif(current_setting('app.task_id', true), '')::uuid
     and l.token   = nullif(current_setting('app.task_token', true), '')::uuid
     and l.lease_until > clock_timestamp()
$$;
create or replace function app.worker_context() returns text
  language sql stable security definer set search_path = app, pg_temp as $$
  select case when l.task_id is null then 'none' when l.customer_id is null then 'acquisition' else 'customer' end
    from (select 1) one
    left join app.task_leases l
      on l.task_id = nullif(current_setting('app.task_id', true), '')::uuid
     and l.token   = nullif(current_setting('app.task_token', true), '')::uuid
     and l.lease_until > clock_timestamp()
$$;

create or replace function app.claim_task(p_agent_id text, p_worker text, p_lease_seconds int default 300, p_max_attempts int default 3)
returns table (task_id uuid, token uuid, fencing bigint, customer_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
#variable_conflict use_column
declare t app.tasks; r record; tok uuid := gen_random_uuid(); f bigint;
begin
  if p_lease_seconds < 10 or p_lease_seconds > 900 then
    raise exception 'LEASE_SECONDS_OUT_OF_RANGE' using errcode = '22023';
  end if;
  -- recovery (lock order: tasks -> task_leases): the expired holder loses its token FIRST, so it can
  -- neither heartbeat, bind, re-queue nor complete; then the task is re-queued or dead-lettered
  for r in select x.id, x.attempts from app.tasks x join app.task_leases l on l.task_id = x.id
            where x.status = 'running' and x.agent_id = p_agent_id and l.lease_until < clock_timestamp()
            for update of x skip locked loop
    update app.task_leases set token = gen_random_uuid(), lease_until = clock_timestamp() where task_leases.task_id = r.id;
    update app.tasks set status = case when r.attempts >= p_max_attempts then 'failed'::app.task_status else 'queued'::app.task_status end,
           error_code = case when r.attempts >= p_max_attempts then 'DEAD_LETTER' else error_code end,
           run_after = clock_timestamp()
     where id = r.id;
  end loop;
  select * into t from app.tasks q
   where q.status = 'queued' and q.agent_id = p_agent_id and q.run_after <= clock_timestamp()
   order by q.priority desc, q.created_at
   for update skip locked limit 1;
  if not found then return; end if;
  update app.tasks set status = 'running', started_at = now(), attempts = attempts + 1 where id = t.id;
  f := nextval('app.fencing_seq');
  insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
  values (t.id, t.customer_id, t.agent_id, tok, p_worker, f, clock_timestamp() + make_interval(secs => p_lease_seconds))
  on conflict (task_id) do update set token = excluded.token, worker = excluded.worker, fencing = excluded.fencing,
     lease_until = excluded.lease_until, heartbeat_at = clock_timestamp(), customer_id = excluded.customer_id;
  return query select t.id, tok, f, t.customer_id;
end $$;

create or replace function app.bind_task(p_task uuid, p_token uuid) returns uuid
language plpgsql security definer set search_path = app, pg_temp as $$
declare c uuid;
begin
  select l.customer_id into c from app.task_leases l
   where l.task_id = p_task and l.token = p_token and l.lease_until > clock_timestamp();
  if not found then raise exception 'LEASE_INVALID' using errcode = '42501'; end if;
  perform set_config('app.task_id', p_task::text, true), set_config('app.task_token', p_token::text, true);
  return c;
end $$;

create or replace function app.extend_task_lease(p_task uuid, p_token uuid, p_seconds int) returns boolean
language sql security definer set search_path = app, pg_temp as $$
  with u as (update app.task_leases l
                set lease_until = clock_timestamp() + make_interval(secs => least(greatest(p_seconds, 10), 900)),
                    heartbeat_at = clock_timestamp()
              where l.task_id = p_task and l.token = p_token and l.lease_until > clock_timestamp()
                and exists (select 1 from app.tasks x where x.id = p_task and x.status = 'running')
             returning 1)
  select exists (select 1 from u);
$$;

create or replace function app.complete_task(p_task uuid, p_token uuid, p_fencing bigint, p_status app.task_status, p_error text default null)
returns boolean language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if p_status not in ('succeeded', 'failed', 'escalated', 'cancelled') then
    raise exception 'TASK_STATUS_NOT_TERMINAL' using errcode = '22023';
  end if;
  perform 1 from app.tasks where id = p_task and status = 'running' for update;
  if not found then return false; end if;
  perform 1 from app.task_leases
   where task_id = p_task and token = p_token and fencing = p_fencing and lease_until > clock_timestamp();
  if not found then return false; end if;                     -- stale, recovered or expired holder
  update app.tasks set status = p_status, error_code = p_error, finished_at = now() where id = p_task;
  update app.task_leases set lease_until = clock_timestamp(), token = gen_random_uuid() where task_id = p_task;
  return true;
end $$;

create or replace function app.requeue_task(p_task uuid, p_token uuid, p_delay_seconds int) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  perform 1 from app.tasks where id = p_task and status = 'running' for update;
  if not found then return false; end if;
  perform 1 from app.task_leases where task_id = p_task and token = p_token and lease_until > clock_timestamp();
  if not found then return false; end if;
  update app.tasks set status = 'queued', run_after = clock_timestamp() + make_interval(secs => greatest(p_delay_seconds, 1)) where id = p_task;
  update app.task_leases set lease_until = clock_timestamp(), token = gen_random_uuid() where task_id = p_task;
  return true;
end $$;

-- ------------------------------------------------------------ T1 the worker creates tasks; only lease functions change them
revoke insert, update on app.tasks from hermes_worker;
grant insert (public_ref, customer_id, agent_id, kind, priority, idempotency_key, run_after) on app.tasks to hermes_worker;

-- ------------------------------------------------------------ W1 webhook ingest routes by channel identity; worker sets processed_at only
alter table app.webhook_events add column channel_external_id text;    -- phone_number_id / page id the provider addressed
create policy channel_accounts_ingest_route on app.channel_accounts for select to hermes_ingest using (status = 'active');
grant select (customer_id, kind, external_id, status) on app.channel_accounts to hermes_ingest;

create or replace function app.webhook_route() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  new.customer_id := null;                                       -- never from the caller
  if new.signature_valid and new.channel_external_id is not null then
    select a.customer_id into new.customer_id from app.channel_accounts a
     where a.kind = new.kind and a.external_id = new.channel_external_id and a.status = 'active';
  end if;
  return new;
end $$;
create trigger webhook_route before insert on app.webhook_events for each row execute function app.webhook_route();

create or replace function app.webhook_enqueue() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if new.customer_id is not null then                            -- unrouted or unsigned events wait for an operator
    insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key)
    values ('t_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 16), new.customer_id, 'agent_triage',
            'inbound.event', 'wh:' || new.kind::text || ':' || new.external_event_id);
  end if;
  return null;
end $$;
create trigger webhook_enqueue after insert on app.webhook_events for each row execute function app.webhook_enqueue();

drop policy webhook_events_ingest_insert on app.webhook_events;
create policy webhook_events_ingest_insert on app.webhook_events for insert to hermes_ingest
  with check (processed_at is null
              and (customer_id is null
                   or (signature_valid and customer_id = (select a.customer_id from app.channel_accounts a
                                                           where a.kind = webhook_events.kind
                                                             and a.external_id = webhook_events.channel_external_id
                                                             and a.status = 'active'))));
revoke insert on app.webhook_events from hermes_ingest;
grant insert (kind, external_event_id, signature_valid, payload, channel_external_id) on app.webhook_events to hermes_ingest;
create policy tasks_ingest_enqueue on app.tasks for insert to hermes_ingest
  with check (customer_id is not null and agent_id = 'agent_triage' and kind = 'inbound.event' and status = 'queued' and attempts = 0);
grant insert (public_ref, customer_id, agent_id, kind, idempotency_key) on app.tasks to hermes_ingest;

revoke update on app.webhook_events from hermes_worker;
grant update (processed_at) on app.webhook_events to hermes_worker;

-- ------------------------------------------------------------ C1 approvals: consumption only by the owner-run outbox trigger
create or replace function app.approvals_before_write() returns trigger
language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.decision <> 'pending' or new.decided_by is not null or new.decided_at is not null
       or new.consumed_at is not null or new.consumed_by_ref is not null then
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
      raise exception 'CUSTOMER_APPROVAL_OWNER_ONLY' using errcode = '42501';
    end if;
    if old.scope = 'platform' and not app.is_operator() then
      raise exception 'PLATFORM_APPROVAL_OPERATOR_AAL2_ONLY' using errcode = '42501';
    end if;
    new.decided_at := now();
  end if;
  if (new.consumed_at, new.consumed_by_ref) is distinct from (old.consumed_at, old.consumed_by_ref) then
    if old.consumed_at is not null then
      raise exception 'APPROVAL_CONSUMED_FINAL' using errcode = 'P0001';
    end if;
    if current_user <> (select pg_catalog.pg_get_userbyid(c.relowner) from pg_catalog.pg_class c where c.oid = 'app.approvals'::regclass) then
      raise exception 'CONSUME_VIA_OUTBOX_ONLY' using errcode = '42501';   -- only app.outbox_before_write runs as the owner
    end if;
  end if;
  return new;
end $$;

-- ------------------------------------------------------------ O1–O3 outbox dispatch state machine
-- pending -> sending (claim, token) -> sent | failed | pending (released BEFORE the request left) | human
-- sending with an EXPIRED lease and no recorded result = ambiguous: re-claimable only for a topic whose
-- provider de-duplicates (provider_idempotent), otherwise only a provider id (reconciliation) or an aal2
-- operator resolves it. A human check is cleared only by an aal2 operator with a recorded resolution.
alter table app.outbox_topics add column provider_idempotent boolean not null default false;
update app.outbox_topics set provider_idempotent = true where topic = 'site.deploy_prod';   -- same content_hash = same deploy
alter table app.outbox add column sending_token uuid;
alter table app.outbox add column failed_at timestamptz;
alter table app.outbox add column resolution text check (resolution in ('confirmed_sent', 'resend', 'abandon'));
alter table app.outbox add column resolved_by uuid;
alter table app.outbox add column resolved_at timestamptz;
alter table app.outbox add constraint outbox_one_outcome check (dispatched_at is null or failed_at is null);
alter table app.outbox add constraint outbox_failure_explained check (failed_at is null or last_error is not null);

create or replace function app.outbox_before_write() returns trigger
language plpgsql security definer set search_path = app, extensions, public, pg_temp as $$
declare t app.outbox_topics; a app.approvals; live boolean;
begin
  if tg_op = 'UPDATE' then
    if (new.topic, new.payload, new.approval_id, new.customer_id, new.target_id) is distinct from
       (old.topic, old.payload, old.approval_id, old.customer_id, old.target_id) then
      raise exception 'OUTBOX_IMMUTABLE' using errcode = 'P0001';
    end if;
    new.payload_hash := old.payload_hash;
    if old.dispatched_at is not null or old.failed_at is not null then
      if (new.dispatched_at, new.failed_at, new.provider_message_id, new.sending_until, new.sending_token, new.needs_human_check, new.resolution)
         is distinct from (old.dispatched_at, old.failed_at, old.provider_message_id, old.sending_until, old.sending_token, old.needs_human_check, old.resolution) then
        raise exception 'OUTBOX_FINAL' using errcode = 'P0001';
      end if;
      return new;
    end if;
    if old.needs_human_check then
      if not new.needs_human_check then
        if not app.is_operator() then raise exception 'OUTBOX_NEEDS_HUMAN' using errcode = '42501'; end if;
        if new.resolution is null then raise exception 'RESOLUTION_REQUIRED' using errcode = 'P0001'; end if;
        new.resolved_by := auth.uid();
        new.resolved_at := now();
        if new.resolution = 'confirmed_sent' then new.dispatched_at := coalesce(new.dispatched_at, now()); end if;
        if new.resolution = 'abandon' then new.failed_at := coalesce(new.failed_at, now()); new.last_error := coalesce(new.last_error, 'ABANDONED'); end if;
        if new.resolution = 'resend' then new.sending_until := null; new.sending_token := null; end if;
        return new;
      end if;
      if (new.dispatched_at, new.failed_at, new.sending_until, new.sending_token) is distinct from
         (old.dispatched_at, old.failed_at, old.sending_until, old.sending_token) then
        raise exception 'OUTBOX_NEEDS_HUMAN' using errcode = '42501';
      end if;
      return new;
    end if;
    live := old.sending_until is not null and old.sending_until > clock_timestamp();
    select * into t from app.outbox_topics where topic = new.topic;
    if new.needs_human_check then                                -- raising the flag is always allowed
      if new.dispatched_at is not null or new.failed_at is not null then raise exception 'OUTBOX_BAD_TRANSITION' using errcode = 'P0001'; end if;
      return new;
    end if;
    if (new.sending_until, new.sending_token) is distinct from (old.sending_until, old.sending_token) then
      if new.sending_until > clock_timestamp() + interval '10 minutes' then
        raise exception 'OUTBOX_LEASE_TOO_LONG' using errcode = 'P0001';
      end if;
      if old.sending_until is null then                          -- claim
        if new.sending_token is null or new.sending_until is null or new.sending_until <= clock_timestamp() then
          raise exception 'OUTBOX_BAD_CLAIM' using errcode = 'P0001';
        end if;
      elsif live then                                            -- heartbeat by the holder, or release before the request left
        -- coalesce: with a NULL last_error or token the test is NULL, and "if not NULL" would let the release through
        if not coalesce((new.sending_token = old.sending_token and new.sending_until > old.sending_until)
                or (new.sending_until is null and new.sending_token is null and new.last_error like 'BEFORE_SEND%'), false) then
          raise exception 'OUTBOX_LEASE_HELD' using errcode = 'P0001';
        end if;
      elsif not coalesce(t.provider_idempotent and new.sending_token is not null and new.sending_until > clock_timestamp(), false) then
        raise exception 'OUTBOX_AMBIGUOUS_NEEDS_HUMAN' using errcode = 'P0001';  -- expired and unconfirmed: sent or not?
      end if;
    end if;
    if new.dispatched_at is not null and old.dispatched_at is null then
      if old.sending_until is null then raise exception 'OUTBOX_NOT_CLAIMED' using errcode = 'P0001'; end if;
      if not live and new.provider_message_id is null then
        raise exception 'OUTBOX_AMBIGUOUS_NEEDS_HUMAN' using errcode = 'P0001';     -- after expiry only provider evidence counts
      end if;
    end if;
    if new.failed_at is not null and old.failed_at is null and old.sending_until is not null and not live then
      raise exception 'OUTBOX_AMBIGUOUS_NEEDS_HUMAN' using errcode = 'P0001';
    end if;
    return new;
  end if;
  -- INSERT (unchanged from 0009 except the consumption guard, which is now ownership-based)
  new.payload_hash := app.canonical_hash(new.payload);
  if new.sending_until is not null or new.sending_token is not null or new.dispatched_at is not null
     or new.failed_at is not null or new.resolution is not null or new.needs_human_check then
    raise exception 'OUTBOX_MUST_START_PENDING' using errcode = 'P0001';
  end if;
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
  update app.approvals set consumed_at = now(), consumed_by_ref = 'outbox:' || new.id where id = a.id;
  return new;
end $$;

-- A redelivered request (same approval, same topic, target and payload) gets the existing row back.
create or replace function app.enqueue_outbox(p_topic text, p_payload jsonb, p_approval_id uuid, p_target_id text)
returns bigint language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
declare o app.outbox; v_id bigint; v_customer uuid;
begin
  if p_approval_id is not null then
    select * into o from app.outbox where approval_id = p_approval_id;
    if found then
      if o.topic = p_topic and o.target_id is not distinct from p_target_id and o.payload_hash = app.canonical_hash(p_payload) then
        return o.id;
      end if;
      raise exception 'APPROVAL_ALREADY_CONSUMED' using errcode = 'P0001';
    end if;
    select customer_id into v_customer from app.approvals where id = p_approval_id;
  else
    v_customer := app.worker_customer_id();
  end if;
  begin
    insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
    values (v_customer, p_topic, p_payload, p_approval_id, p_target_id) returning id into v_id;
    return v_id;
  exception when unique_violation or raise_exception then     -- a concurrent delivery consumed it first
    if p_approval_id is null or (sqlstate <> '23505' and sqlerrm <> 'APPROVAL_ALREADY_CONSUMED') then raise; end if;
    select * into o from app.outbox where approval_id = p_approval_id;
    if found and o.topic = p_topic and o.target_id is not distinct from p_target_id and o.payload_hash = app.canonical_hash(p_payload) then
      return o.id;
    end if;
    raise;
  end;
end $$;

drop function app.claim_outbox_dispatch(bigint, int);
create function app.claim_outbox_dispatch(p_id bigint, p_lease_seconds int default 120, p_max_attempts int default 5) returns uuid
language plpgsql security invoker set search_path = app, pg_temp as $$
declare o app.outbox; a app.approvals; t app.outbox_topics; tok uuid := gen_random_uuid();
begin
  select * into o from app.outbox where id = p_id for update skip locked;
  if not found or o.dispatched_at is not null or o.failed_at is not null or o.needs_human_check then return null; end if;
  select * into t from app.outbox_topics where topic = o.topic;
  if o.sending_until is not null then
    if o.sending_until > clock_timestamp() then return null; end if;           -- a live dispatcher holds it
    if not t.provider_idempotent then                                          -- sent or not? never re-sent automatically
      update app.outbox set needs_human_check = true, last_error = 'AMBIGUOUS_LEASE_EXPIRED' where id = o.id;
      return null;
    end if;
  end if;
  if o.attempts >= p_max_attempts then
    update app.outbox set needs_human_check = true, last_error = 'MAX_ATTEMPTS' where id = o.id;
    return null;
  end if;
  if t.requires_approval then
    select * into a from app.approvals where id = o.approval_id;
    if not found or a.decision <> 'approved' or a.consumed_by_ref is distinct from ('outbox:' || o.id)
       or a.payload_hash is distinct from o.payload_hash then
      update app.outbox set needs_human_check = true, last_error = 'APPROVAL_REVERIFY_FAILED' where id = o.id;
      return null;
    end if;
  end if;
  update app.outbox set sending_until = clock_timestamp() + make_interval(secs => least(greatest(p_lease_seconds, 10), 600)),
                        sending_token = tok, attempts = attempts + 1
   where id = o.id;
  return tok;
end $$;

-- outcome of one send, reported by the holder of the claim token
create or replace function app.finish_outbox_dispatch(p_id bigint, p_token uuid, p_outcome text, p_provider_ref text default null, p_error text default null)
returns boolean language plpgsql security invoker set search_path = app, pg_temp as $$
declare o app.outbox;
begin
  if p_outcome not in ('sent', 'failed_permanent', 'failed_before_send', 'ambiguous') then
    raise exception 'OUTBOX_OUTCOME_UNKNOWN' using errcode = '22023';
  end if;
  select * into o from app.outbox where id = p_id for update;
  if not found or o.sending_token is distinct from p_token then return false; end if;
  if p_outcome = 'sent' then
    update app.outbox set dispatched_at = now(), provider_message_id = p_provider_ref where id = p_id;
  elsif p_outcome = 'failed_permanent' then
    update app.outbox set failed_at = now(), last_error = coalesce(left(p_error, 200), 'FAILED_PERMANENT') where id = p_id;
  elsif p_outcome = 'failed_before_send' then                  -- the request never left: safe to claim again later
    update app.outbox set sending_until = null, sending_token = null, last_error = 'BEFORE_SEND:' || coalesce(left(p_error, 180), '') where id = p_id;
  else
    update app.outbox set needs_human_check = true, last_error = 'AMBIGUOUS:' || coalesce(left(p_error, 180), '') where id = p_id;
  end if;
  return true;
end $$;

-- provider status webhooks: evidence that an expired send did go out
create or replace function app.reconcile_outbox_sent(p_id bigint, p_provider_ref text) returns boolean
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if p_provider_ref is null or length(p_provider_ref) < 4 then raise exception 'PROVIDER_REF_REQUIRED' using errcode = 'P0001'; end if;
  update app.outbox set dispatched_at = now(), provider_message_id = p_provider_ref
   where id = p_id and dispatched_at is null and failed_at is null and not needs_human_check and sending_until is not null;
  return found;
end $$;

-- an aal2 operator settles an ambiguous row: confirmed_sent | resend | abandon, with a reason
create or replace function app.resolve_outbox(p_id bigint, p_resolution text, p_reason text) returns void
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if not app.is_operator() then raise exception 'OPERATOR_AAL2_ONLY' using errcode = '42501'; end if;
  if coalesce(length(trim(p_reason)), 0) < 5 then raise exception 'REASON_REQUIRED' using errcode = 'P0001'; end if;
  update app.outbox set needs_human_check = false, resolution = p_resolution, last_error = 'RESOLVED: ' || left(p_reason, 180)
   where id = p_id and needs_human_check;
  if not found then raise exception 'OUTBOX_NOT_WAITING_FOR_HUMAN' using errcode = 'P0001'; end if;
end $$;

-- what an operator must look at: flagged rows and sends whose lease expired without a result
create view app.v_outbox_attention with (security_invoker = true) as
  select id, customer_id, topic, attempts, last_error, sending_until, needs_human_check,
         case when needs_human_check then 'needs_human_check' else 'sending_expired' end as reason
    from app.outbox
   where dispatched_at is null and failed_at is null
     and (needs_human_check or (sending_until is not null and sending_until < now()));
grant select on app.v_outbox_attention to authenticated;

revoke update on app.outbox from hermes_worker;
grant update (sending_until, sending_token, attempts, dispatched_at, provider_message_id, last_error, needs_human_check, failed_at)
  on app.outbox to hermes_worker;
grant execute on function app.enqueue_outbox(text, jsonb, uuid, text), app.claim_outbox_dispatch(bigint, int, int),
  app.finish_outbox_dispatch(bigint, uuid, text, text, text), app.reconcile_outbox_sent(bigint, text) to hermes_worker;
grant execute on function app.resolve_outbox(bigint, text, text) to authenticated;

-- ------------------------------------------------------------ A1 audit: written by the database inside the effect's transaction
create or replace function app.audit_effect() returns trigger
language plpgsql security definer set search_path = app, pg_temp as $$
declare act text; tgt text; cust uuid; det jsonb := '{}'::jsonb; who text; kind app.actor_type;
begin
  if tg_table_name = 'approvals' then
    if new.decision is not distinct from old.decision then return null; end if;
    act := 'approval.' || new.decision::text; tgt := 'approval:' || new.id; cust := new.customer_id;
    det := jsonb_build_object('proposal_action', new.proposal_action, 'target_id', new.target_id, 'payload_hash', new.payload_hash);
  elsif tg_table_name = 'outbox' then
    if tg_op = 'INSERT' then act := 'outbox.enqueued';
    elsif old.needs_human_check and not new.needs_human_check then act := 'outbox.resolved';     -- before sent/failed:
    elsif new.dispatched_at is not null and old.dispatched_at is null then act := 'outbox.sent'; -- a resolution may set them
    elsif new.failed_at is not null and old.failed_at is null then act := 'outbox.failed';
    elsif new.needs_human_check and not old.needs_human_check then act := 'outbox.needs_human';
    else return null;
    end if;
    tgt := 'outbox:' || new.id; cust := new.customer_id;
    det := jsonb_strip_nulls(jsonb_build_object('topic', new.topic, 'approval_id', new.approval_id, 'payload_hash', new.payload_hash,
             'provider_message_id', new.provider_message_id, 'resolution', new.resolution, 'last_error', left(new.last_error, 120)));
  elsif tg_table_name = 'content_items' then
    if new.status is distinct from 'published' or (tg_op = 'UPDATE' and old.status = 'published') then return null; end if;
    act := 'content.published'; tgt := 'content:' || new.id; cust := new.customer_id;
    det := jsonb_build_object('approval_id', new.approval_id);
  elsif tg_table_name = 'deployments' then
    if new.env <> 'prod' then return null; end if;
    act := case when new.rollback_of is null then 'deploy.prod' else 'deploy.rollback' end;
    tgt := 'deployment:' || new.id; cust := new.customer_id;
    det := jsonb_strip_nulls(jsonb_build_object('site_id', new.site_id, 'content_hash', new.content_hash,
             'approval_id', new.approval_id, 'rollback_of', new.rollback_of));
  else
    return null;
  end if;
  if auth.uid() is not null then
    who := auth.uid()::text;
    kind := case when app.is_operator() then 'operator'::app.actor_type else 'user'::app.actor_type end;
  elsif app.worker_context() <> 'none' then
    select l.agent_id into who from app.task_leases l where l.task_id = nullif(current_setting('app.task_id', true), '')::uuid;
    kind := 'agent';
  else
    who := session_user; kind := 'system';
  end if;
  insert into app.audit_log (actor_type, actor_id, customer_id, action, target, details)
  values (kind, coalesce(who, 'unknown'), cust, act, tgt, det);
  return null;
end $$;
create trigger approvals_audit after update on app.approvals for each row execute function app.audit_effect();
create trigger outbox_audit after insert or update on app.outbox for each row execute function app.audit_effect();
create trigger content_items_audit after insert or update on app.content_items for each row execute function app.audit_effect();
create trigger deployments_audit after insert on app.deployments for each row execute function app.audit_effect();

drop policy audit_insert_authenticated on app.audit_log;
create policy audit_insert_authenticated on app.audit_log for insert to authenticated
  with check (actor_id = ((select auth.uid())::text)
              and ((actor_type = 'operator' and (select app.is_operator()))
                or (actor_type = 'user' and customer_id in (select app.current_user_customer_ids()))));
drop policy audit_insert_worker on app.audit_log;
create policy audit_insert_worker on app.audit_log for insert to hermes_worker
  with check (actor_type = 'agent'
              and (customer_id = (select app.worker_customer_id())
                   or (customer_id is null and (select app.worker_context()) = 'acquisition')));

-- ------------------------------------------------------------ R1 retention: inquiry bodies after 30 days (C4.7)
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_jobs') then create role hermes_jobs nologin noinherit; end if;
end $$;
grant usage on schema app to hermes_jobs;
alter table app.inquiries add column body_purged_at timestamptz;
-- no "body_purged_at is null" here: an UPDATE that reads columns must also leave a row the SELECT policy
-- admits, and the purged row has body_purged_at set. The job still never reads a body (column grant below).
create policy inquiries_retention_select on app.inquiries for select to hermes_jobs
  using (received_at < now() - interval '30 days');
create policy inquiries_retention_purge on app.inquiries for update to hermes_jobs
  using (body_purged_at is null and received_at < now() - interval '30 days')
  with check (body is null and body_purged_at is not null);
grant select (id, received_at, body_purged_at) on app.inquiries to hermes_jobs;    -- never the body itself
grant update (body, body_purged_at) on app.inquiries to hermes_jobs;

create table app.retention_runs (
  id              bigint generated always as identity primary key,
  job             text not null check (job in ('inquiry_body_30d')),
  rows_affected   int not null check (rows_affected >= 0),
  ran_at          timestamptz not null default now()
);
alter table app.retention_runs enable row level security;
alter table app.retention_runs force row level security;
create policy retention_runs_operator_select on app.retention_runs for select to authenticated using ((select app.is_operator()));
create policy retention_runs_jobs_insert on app.retention_runs for insert to hermes_jobs with check (true);
grant select on app.retention_runs to authenticated;
grant insert (job, rows_affected) on app.retention_runs to hermes_jobs;

create or replace function app.purge_inquiry_bodies() returns int
language plpgsql security invoker set search_path = app, pg_temp as $$
declare n int;
begin
  update app.inquiries set body = null, body_purged_at = now()
   where body_purged_at is null and received_at < now() - interval '30 days';
  get diagnostics n = row_count;
  insert into app.retention_runs (job, rows_affected) values ('inquiry_body_30d', n);
  return n;
end $$;
grant execute on function app.purge_inquiry_bodies() to hermes_jobs;

create view app.v_retention_status with (security_invoker = true) as
  select (select max(ran_at) from app.retention_runs where job = 'inquiry_body_30d') as last_run_at,
         (select count(*) from app.inquiries where body is not null and received_at < now() - interval '31 days') as overdue_bodies;
grant select on app.v_retention_status to authenticated;

commit;
