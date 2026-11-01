-- 0034 · the owner answers a customer from the portal (spec 28.41)
-- A message with no approved answer was escalated to the owner, who could only answer outside Hermes, and on a
-- number served by the Cloud API that may not be possible at all. Now the owner writes the answer in the portal,
-- the same way as a post (0023): the owner writes, the worker checks the text and proposes it as reply:send to the
-- customer who wrote, the owner confirms it, and the outbox sends it once. No new definer function, no copy of the
-- customer's address: the worker reads the addressee from the message's own event (purged after 30 days, 0030).
--   the reply the owner may write   owner_replies_owner_insert: own business, own login, a message of the last 23 hours
--                                   (inside WhatsApp's 24-hour window) on a channel that can be answered
--   the proposal                    the owner queues reply.owner (tasks_owner_reply) for a reply of their own
--   what the worker may do          read and mark its leased business's replies (status only)
begin;

create table app.owner_replies (
  id          uuid primary key default gen_random_uuid(),
  customer_id uuid not null references app.customers(id),
  inquiry_id  uuid not null references app.inquiries(id),
  body        text not null check (length(btrim(body)) between 1 and 1000),
  written_by  uuid not null,
  status      text not null default 'queued' check (status in ('queued', 'proposed', 'sent', 'refused', 'failed')),
  created_at  timestamptz not null default now()
);
create index owner_replies_customer on app.owner_replies (customer_id);
alter table app.owner_replies enable row level security;
alter table app.owner_replies force row level security;

create policy owner_replies_owner_insert on app.owner_replies for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()) and written_by = (select auth.uid()) and status = 'queued'
              and exists (select 1 from app.inquiries i where i.id = inquiry_id and i.customer_id = owner_replies.customer_id
                            and i.received_at > now() - interval '23 hours' and i.event_ref is not null
                            and i.source::text in ('whatsapp', 'facebook', 'instagram', 'email')));
create policy owner_replies_owner_select on app.owner_replies for select to authenticated
  using (customer_id in (select app.current_user_customer_ids()));
create policy owner_replies_operator_select on app.owner_replies for select to authenticated using ((select app.is_operator()));
create policy owner_replies_worker_select on app.owner_replies for select to hermes_worker
  using (customer_id = (select app.worker_customer_id()));
create policy owner_replies_worker_update on app.owner_replies for update to hermes_worker
  using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select on app.owner_replies to authenticated;
grant insert (customer_id, inquiry_id, body, written_by) on app.owner_replies to authenticated;
grant select, update (status) on app.owner_replies to hermes_worker;

create policy tasks_owner_reply on app.tasks for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()) and agent_id = 'agent_triage'
              and kind = 'reply.owner' and status = 'queued' and attempts = 0
              and exists (select 1 from app.owner_replies r where 'owner_reply:' || r.id::text = idempotency_key
                            and r.customer_id = tasks.customer_id and r.status = 'queued'));

commit;
