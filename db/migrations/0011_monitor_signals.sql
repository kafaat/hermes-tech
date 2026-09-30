-- 0011 · health signals for an external monitor (numbers, never rows)
-- Railway's healthcheck runs at deploy time only and its cron never reports a skipped or hung run, so what
-- must stay true between deploys is read from outside: GET /deps on hermes-app calls app.health_signals()
-- as hermes_monitor and an uptime monitor alerts on anything but 200.
--
-- Two roles, so that the caller holds nothing but EXECUTE:
--   hermes_monitor         the caller. EXECUTE on one function; no table, column or policy of its own.
--   hermes_monitor_reader  owns the function (SECURITY DEFINER runs as it). NOLOGIN; column grants on
--                          timestamps and flags only, never content or identifiers of a customer's data.
-- The function is not owned by the migration owner on purpose: the tables are FORCE'd, so an owner-run
-- definer would see nothing without owner-wide policies, and those would open every other definer too.
begin;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_monitor') then create role hermes_monitor nologin noinherit; end if;
  if not exists (select 1 from pg_roles where rolname = 'hermes_monitor_reader') then create role hermes_monitor_reader nologin noinherit; end if;
end $$;
grant usage on schema app to hermes_monitor, hermes_monitor_reader;

grant select (job, ran_at) on app.retention_runs to hermes_monitor_reader;
create policy retention_runs_monitor_read on app.retention_runs for select to hermes_monitor_reader using (true);

grant select (received_at, body_purged_at) on app.inquiries to hermes_monitor_reader;          -- never the body
create policy inquiries_monitor_read on app.inquiries for select to hermes_monitor_reader
  using (body_purged_at is null and received_at < now() - interval '31 days');

grant select (dispatched_at, failed_at, needs_human_check, sending_until) on app.outbox to hermes_monitor_reader;
create policy outbox_monitor_read on app.outbox for select to hermes_monitor_reader
  using (dispatched_at is null and failed_at is null);

grant select (customer_id, signature_valid, received_at, processed_at) on app.webhook_events to hermes_monitor_reader;
create policy webhook_events_monitor_read on app.webhook_events for select to hermes_monitor_reader
  using (processed_at is null);

-- retention_age_seconds  since the last recorded purge run (null: never ran). The monitor alerts above 26 h.
-- overdue_bodies         inquiry bodies older than 31 days still present (the purge is not keeping up)
-- outbox_attention       rows an operator must resolve: needs_human_check, or a send whose lease expired
-- webhook_backlog        routed, signed events unprocessed for more than 5 minutes (the worker is stuck)
-- webhook_unrouted       signed events no channel account claims; they wait for an operator (information)
create function app.health_signals()
returns table (retention_age_seconds bigint, overdue_bodies bigint, outbox_attention bigint,
               webhook_backlog bigint, webhook_unrouted bigint)
language sql stable security definer set search_path = app, pg_temp as $$
  select extract(epoch from now() - (select max(ran_at) from app.retention_runs where job = 'inquiry_body_30d'))::bigint,
         (select count(*) from app.inquiries),
         (select count(*) from app.outbox where needs_human_check or (sending_until is not null and sending_until < now())),
         (select count(*) from app.webhook_events where customer_id is not null and received_at < now() - interval '5 minutes'),
         (select count(*) from app.webhook_events where customer_id is null and signature_valid)
$$;
revoke execute on function app.health_signals() from public;
grant execute on function app.health_signals() to hermes_monitor;
-- handing the function over needs the owner to be a member of the new owner, and the new owner to hold CREATE
-- on the schema; both end right after (ownership stays)
grant hermes_monitor_reader to current_user;
grant create on schema app to hermes_monitor_reader;
alter function app.health_signals() owner to hermes_monitor_reader;
revoke create on schema app from hermes_monitor_reader;
do $$ begin execute format('revoke hermes_monitor_reader from %I', current_user); end $$;

commit;
