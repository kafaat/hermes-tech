-- 0012 · when the next purge is due, decided in the database, not by whoever reads /deps
-- 0011 reported "never ran" as stale at once, so a new environment alerted for up to a day before its first scheduled
-- purge. The due time is now coalesce(last success, start of monitoring) + 26 h: the daily run plus two hours of
-- slack. The start of monitoring is recorded once, when this migration runs. The policy (26 h) lives here only.
-- retention_runs rows are written inside the purge's own transaction (0010), so the latest row is the latest
-- success (SQL case 45b); a failed run moves nothing.
begin;

create table app.monitor_epoch (
  id          boolean primary key default true check (id),         -- one row
  started_at  timestamptz not null default now()
);
insert into app.monitor_epoch default values;
alter table app.monitor_epoch enable row level security;
alter table app.monitor_epoch force row level security;
create policy monitor_epoch_reader on app.monitor_epoch for select to hermes_monitor_reader using (true);
grant select (started_at) on app.monitor_epoch to hermes_monitor_reader;

-- replacing the function (new columns) needs its owner's rights: the same short membership as in 0011
grant hermes_monitor_reader to current_user;
drop function app.health_signals();
create function app.health_signals()
returns table (retention_age_seconds bigint, retention_due_at timestamptz, retention_stale boolean,
               overdue_bodies bigint, outbox_attention bigint, webhook_backlog bigint, webhook_unrouted bigint)
language sql stable security definer set search_path = app, pg_temp as $$
  with r as (select (select max(ran_at) from app.retention_runs where job = 'inquiry_body_30d') as last_ok,
                    (select started_at from app.monitor_epoch) as epoch)
  select extract(epoch from now() - r.last_ok)::bigint,
         coalesce(r.last_ok, r.epoch) + interval '26 hours',
         now() > coalesce(r.last_ok, r.epoch) + interval '26 hours',
         (select count(*) from app.inquiries),
         (select count(*) from app.outbox where needs_human_check or (sending_until is not null and sending_until < now())),
         (select count(*) from app.webhook_events where customer_id is not null and received_at < now() - interval '5 minutes'),
         (select count(*) from app.webhook_events where customer_id is null and signature_valid)
    from r
$$;
revoke execute on function app.health_signals() from public;
grant execute on function app.health_signals() to hermes_monitor;
grant create on schema app to hermes_monitor_reader;
alter function app.health_signals() owner to hermes_monitor_reader;
revoke create on schema app from hermes_monitor_reader;
do $$ begin execute format('revoke hermes_monitor_reader from %I', current_user); end $$;

commit;
