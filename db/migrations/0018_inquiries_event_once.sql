-- 0018 · one inquiry per inbound message, however often its task runs
-- Left open in 0014 (spec 28.9): a re-run task (lease expired, worker restarted, deploy overlap) inserted the same
-- customer message into app.inquiries a second time. No external effect, but the owner's count and the measurement
-- views (0004) counted it twice. An inquiry now carries the provider's message id (event_ref, the wamid) and the
-- database keeps one per (customer, message): service/worker.py inserts with ON CONFLICT DO NOTHING. Rows from before
-- this migration have no event_ref and stay as they are (a history, not rewritten).
begin;

alter table app.inquiries
  add column event_ref text check (event_ref is null or (length(event_ref) between 1 and 200));

create unique index inquiries_event_once on app.inquiries (customer_id, event_ref) where event_ref is not null;

commit;
