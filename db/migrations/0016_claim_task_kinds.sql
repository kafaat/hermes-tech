-- 0016 · a worker claims only the task kinds it knows
-- Found on staging (spec 28.9): during a deploy's overlap the OLD instance claimed the new outbox.resend task, read its
-- key as an inbound event and failed before sending; the lease then held the task for two minutes until the new
-- instance took it. The outcome was right (sent once) but late, and any future kind would repeat it. claim_task now
-- takes the kinds the caller handles (p_kinds, null = every kind, as before); service/worker.py passes its own list,
-- so from this version on a worker leaves unknown kinds to a worker that knows them. Recovery of expired leases is
-- unchanged (it re-queues, it does not claim).
begin;

drop function app.claim_task(text, text, int, int);
create function app.claim_task(p_agent_id text, p_worker text, p_lease_seconds int default 300, p_max_attempts int default 3,
                               p_kinds text[] default null)
returns table (task_id uuid, token uuid, fencing bigint, customer_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
#variable_conflict use_column
declare t app.tasks; r record; tok uuid := gen_random_uuid(); f bigint;
begin
  if p_lease_seconds < 10 or p_lease_seconds > 900 then
    raise exception 'LEASE_SECONDS_OUT_OF_RANGE' using errcode = '22023';
  end if;
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
     and (p_kinds is null or q.kind = any(p_kinds))
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
revoke execute on function app.claim_task(text, text, int, int, text[]) from public;
grant execute on function app.claim_task(text, text, int, int, text[]) to hermes_worker;

commit;
