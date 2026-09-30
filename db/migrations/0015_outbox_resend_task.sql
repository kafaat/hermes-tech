-- 0015 · an operator's "resend" queues the task that sends it
-- resolve_outbox(…, 'resend', …) cleared the row's send claim (0010) but nothing ever took it again: the worker only
-- dispatches inside a task, and the task that made the row had already finished. The resend now enqueues its own
-- task (agent_triage, kind outbox.resend, key resend:<outbox id>:<attempts>) in the same transaction as the decision,
-- so a resend is carried out or visibly waiting, never silently parked. The operator inserts it under the existing
-- tasks_operator_all policy (aal2); a platform row (no customer) has no worker context and is refused.
begin;

create or replace function app.resolve_outbox(p_id bigint, p_resolution text, p_reason text) returns void
language plpgsql security invoker set search_path = app, pg_temp as $$
declare v_customer uuid; v_attempts int;
begin
  if not app.is_operator() then raise exception 'OPERATOR_AAL2_ONLY' using errcode = '42501'; end if;
  if coalesce(length(trim(p_reason)), 0) < 5 then raise exception 'REASON_REQUIRED' using errcode = 'P0001'; end if;
  update app.outbox set needs_human_check = false, resolution = p_resolution, last_error = 'RESOLVED: ' || left(p_reason, 180)
   where id = p_id and needs_human_check
  returning customer_id, attempts into v_customer, v_attempts;
  if not found then raise exception 'OUTBOX_NOT_WAITING_FOR_HUMAN' using errcode = 'P0001'; end if;
  if p_resolution = 'resend' then
    if v_customer is null then raise exception 'OUTBOX_RESEND_NEEDS_CUSTOMER' using errcode = 'P0001'; end if;
    insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key)
    values ('t_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 16), v_customer, 'agent_triage', 'outbox.resend',
            'resend:' || p_id || ':' || v_attempts);
  end if;
end $$;

commit;
