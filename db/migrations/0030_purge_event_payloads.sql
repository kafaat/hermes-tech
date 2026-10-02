-- 0030 · the raw copy of every inbound message is wiped after 30 days, like the inquiry's text (C4.7, spec 28.32)
-- Found in a review of what each new channel stores: app.purge_inquiry_bodies wiped inquiries.body after 30 days, but
-- webhook_events.payload keeps the provider's raw message for ever: the WhatsApp text and number, the Messenger and
-- Instagram text, the site form's name and phone, an email's sender, subject and text. The 30-day promise held for one
-- copy only. Now the same daily job also replaces every payload older than 30 days with {"purged": true}; the row
-- stays (kind and provider id keep a redelivery a duplicate, routing and processing times stay for the monitor).
-- hermes_jobs wipes without reading: no SELECT on payload, as for inquiries.body (0010).
begin;

alter table app.webhook_events add column payload_purged_at timestamptz;

alter table app.retention_runs drop constraint retention_runs_job_check;
alter table app.retention_runs add constraint retention_runs_job_check check (job in ('inquiry_body_30d', 'event_payload_30d'));

create policy webhook_events_retention_select on app.webhook_events for select to hermes_jobs      -- the updated row is
  using (received_at < now() - interval '30 days');                                                 -- read back (0010)
create policy webhook_events_retention_purge on app.webhook_events for update to hermes_jobs
  using (payload_purged_at is null and received_at < now() - interval '30 days') with check (true);
grant select (id, received_at, payload_purged_at) on app.webhook_events to hermes_jobs;              -- never the payload
grant update (payload, payload_purged_at) on app.webhook_events to hermes_jobs;

create or replace function app.purge_inquiry_bodies() returns int
language plpgsql security invoker set search_path = app, pg_temp as $$
declare n int; m int;
begin
  update app.inquiries set body = null, body_purged_at = now()
   where body_purged_at is null and received_at < now() - interval '30 days';
  get diagnostics n = row_count;
  insert into app.retention_runs (job, rows_affected) values ('inquiry_body_30d', n);
  update app.webhook_events set payload = '{"purged": true}'::jsonb, payload_purged_at = now()
   where payload_purged_at is null and received_at < now() - interval '30 days';
  get diagnostics m = row_count;
  insert into app.retention_runs (job, rows_affected) values ('event_payload_30d', m);
  return n;
end $$;

commit;
