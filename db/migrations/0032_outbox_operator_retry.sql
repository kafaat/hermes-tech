-- 0032: an exhausted outbox gets one extra attempt per explicit operator resend.
-- Keep lifetime attempts intact. The existing atomic resolve+task insert (0015)
-- remains the authorization path; duplicate decisions cannot accumulate credit.
begin;
alter table app.outbox add column operator_attempt_limit integer not null default 0
  check (operator_attempt_limit >= 0);

create function app.outbox_retry_allowance_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.operator_attempt_limit <> 0 then
      raise exception 'OUTBOX_RETRY_ALLOWANCE_PROTECTED' using errcode = '42501';
    end if;
    return new;
  end if;
  if new.operator_attempt_limit is distinct from old.operator_attempt_limit then
    raise exception 'OUTBOX_RETRY_ALLOWANCE_PROTECTED' using errcode = '42501';
  end if;
  -- Runs after the existing guard, which validates the operator, stamps identity
  -- and reason, and clears the old claim. Never grant an automatic retry of an
  -- unresolved/expired send. A fresh operator decision is still required.
  if old.needs_human_check and not new.needs_human_check
     and new.resolution = 'resend'
     and old.dispatched_at is null and old.failed_at is null
     and old.attempts >= old.operator_attempt_limit
     and (old.last_error = 'MAX_ATTEMPTS' or old.attempts >= 5) then
    -- Keep the restricted authority helper out of ordinary worker updates.
    -- SQL boolean expressions may reorder evaluation; PL/pgSQL IF may not.
    if app.is_operator() then
      new.operator_attempt_limit := old.attempts + 1;
    end if;
  end if;
  return new;
end $$;
create trigger zz_outbox_retry_allowance before insert or update on app.outbox
for each row execute function app.outbox_retry_allowance_guard();

-- p_max_attempts may lower the baseline, never enlarge it without authority.
create or replace function app.claim_outbox_dispatch(p_id bigint, p_lease_seconds int default 120, p_max_attempts int default 5) returns uuid
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
  if o.attempts >= greatest(least(coalesce(p_max_attempts, 5), 5), o.operator_attempt_limit) then
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

commit;
