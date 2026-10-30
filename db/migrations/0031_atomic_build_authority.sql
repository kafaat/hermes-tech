-- Activation is an explicit owner/deployment operation, never worker registration.
-- A rollback uses a NEW generation with the old commit. Stale heartbeats cannot
-- restore authority. Existing leases drain; this gate controls NEW claims only.
begin;
alter table app.service_build add column generation bigint not null default 0 check (generation >= 0);
revoke all on app.service_build from hermes_worker;
revoke insert (id, commit, started_at, seen_at), update (commit, started_at, seen_at)
  on app.service_build from hermes_worker;
drop policy service_build_worker on app.service_build;
create policy service_build_worker on app.service_build for select to hermes_worker using (id = 1);
grant select on app.service_build to hermes_worker;
-- SECURITY DEFINER must work under a plain, FORCE-RLS migration owner too.
do $$ begin
  execute format('create policy service_build_authority on app.service_build for all to %I using (true) with check (true)', current_user);
end $$;

create function app.activate_service_build(p_commit text, p_generation bigint) returns void
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if p_commit is null or p_commit !~ '^[0-9a-f]{40}$' or p_generation is null or p_generation <= 0 then
    raise exception 'INVALID_BUILD_IDENTITY' using errcode = '22023';
  end if;
  insert into app.service_build(id, commit, generation, started_at, seen_at)
  values (1, p_commit, p_generation, clock_timestamp(), clock_timestamp())
  on conflict (id) do update set commit = excluded.commit, generation = excluded.generation,
    started_at = excluded.started_at, seen_at = excluded.seen_at
  where service_build.generation < excluded.generation;
  if not found then raise exception 'BUILD_GENERATION_NOT_INCREASING' using errcode = '22023'; end if;
end $$;
revoke all on function app.activate_service_build(text, bigint) from public, hermes_worker;

create function app.heartbeat_service_build(p_commit text, p_generation bigint) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
begin
  update app.service_build set seen_at = clock_timestamp()
  where id = 1 and commit = p_commit and generation = p_generation and generation > 0;
  return found;
end $$;
revoke all on function app.heartbeat_service_build(text, bigint) from public;
grant execute on function app.heartbeat_service_build(text, bigint) to hermes_worker;

-- Keep the legacy overload owner-only for administrative compatibility; workers cannot bypass the gate.
revoke all on function app.claim_task(text, text, int, int, text[]) from public, hermes_worker;
create function app.claim_task(p_agent_id text, p_worker text, p_lease_seconds int, p_max_attempts int,
                              p_kinds text[], p_commit text, p_generation bigint)
returns table (task_id uuid, token uuid, fencing bigint, customer_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
#variable_conflict use_column
declare t app.tasks; r record; tok uuid := gen_random_uuid(); f bigint;
begin
  -- Lock the singleton even if the caller is stale. Activation's UPDATE conflicts
  -- with this SHARE lock; validation and claim have one transaction/linearization.
  perform 1 from app.service_build where id = 1 for share;
  -- No row means no lock: never authorize against a row inserted after this read.
  if not found then return; end if;
  if not exists(select 1 from app.service_build where id = 1 and commit = p_commit
                and generation = p_generation and generation > 0) then return; end if;
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
revoke all on function app.claim_task(text, text, int, int, text[], text, bigint) from public;
grant execute on function app.claim_task(text, text, int, int, text[], text, bigint) to hermes_worker;
commit;
