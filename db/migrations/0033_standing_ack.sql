-- 0033 · the instant acknowledgement (spec 28.40): a fourth standing topic, 'ack'.
-- A message with no approved answer waits for the owner; a customer who hears nothing for hours writes elsewhere.
-- The owner approves one acknowledgement text ("we received your message; the owner will answer soon") and grants
-- it as a standing approval, exactly as for hours, location and payment (0020-0022): the database decides, the
-- worker only proposes, the owner's switch (0022) pauses it, and a changed text needs a new grant (0021). What the
-- worker may send under it is fixed by the worker (service/worker.py): never to a complaint, never to unverified
-- mail, at most once a day to the same sender.
begin;
alter table app.standing_approvals drop constraint standing_approvals_topic_check;
alter table app.standing_approvals add constraint standing_approvals_topic_check
  check (topic in ('hours', 'location', 'payment', 'ack'));
commit;
