-- 0027 · build fence (spec 28.29): during a deploy, a worker of the previous build stops taking tasks
-- Found on staging (0026): Railway keeps the previous deployment running until the new one is healthy and drained, about
-- 80 s; meanwhile the previous worker claimed a task and handled it with the previous code (a notice without the new
-- address list). Every task kind is idempotent and leased, so this was a stale behaviour, not a double effect; but a
-- change of behaviour must not be undone by the build it replaces.
-- At start each instance records its commit as the live build and renews seen_at while it runs. A worker claims only
-- while the live build is its own, or while the live build has not been seen for 60 s (a new build that crashed must
-- never stall the queue: the previous build then carries on).
begin;

create table app.service_build (
  id         smallint primary key check (id = 1),           -- one row: the build that should take tasks
  commit     text not null check (commit ~ '^[0-9a-f]{0,40}$'),
  started_at timestamptz not null default now(),
  seen_at    timestamptz not null default now()
);
alter table app.service_build enable row level security;
alter table app.service_build force row level security;
create policy service_build_worker on app.service_build for all to hermes_worker using (id = 1) with check (id = 1);
grant select, insert (id, commit, started_at, seen_at), update (commit, started_at, seen_at) on app.service_build to hermes_worker;

commit;
