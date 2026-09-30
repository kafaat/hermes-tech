-- 0013 · an index leading with customer_id on every table whose policies filter by it
-- Five tables filtered by customer_id in their policies had no such index (audit_log, outbox, webhook_events,
-- idempotency_keys, task_budget). Measured on 200,000 audit_log rows over 100 tenants, an owner session counting its
-- own rows (PG16): no index 25 ms (sequential scan); index alone 25 ms, unused, because the owner and operator
-- policies combine with OR and the planner cannot take the index from the policy; index AND an explicit
-- "where customer_id = $1" in the query 5 ms (bitmap index scan). Both halves matter: the index here, the explicit
-- filter in every query that reads a tenant's rows (the owner portal included). SQL case 48 keeps new tables honest.
begin;
create index audit_log_customer on app.audit_log (customer_id);
create index outbox_customer on app.outbox (customer_id);
create index webhook_events_customer on app.webhook_events (customer_id);
create index idempotency_keys_customer on app.idempotency_keys (customer_id);
create index task_budget_customer on app.task_budget (customer_id);
commit;
