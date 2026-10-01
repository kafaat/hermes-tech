-- Run after 0000 and ALL migrations (0001..0010) on a disposable database, as superuser:
--   psql -v ON_ERROR_STOP=1 -f db/tests/rls_isolation_test.sql
-- Every block must end with NOTICE 'PASS …'. Any EXCEPTION is a failure.
begin;
insert into app.customers (id, public_ref, business_name, sector, city, currency_zone, status) values
  ('00000000-0000-0000-0000-00000000000a', 'cust_aaaa', 'مطعم أ', 'restaurant', 'city_1', 'zone_a', 'active'),
  ('00000000-0000-0000-0000-00000000000b', 'cust_bbbb', 'ورشة ب', 'home_services', 'city_1', 'zone_a', 'active');
insert into app.customer_users (customer_id, auth_user_id, role) values
  ('00000000-0000-0000-0000-00000000000a', '11111111-1111-1111-1111-111111111111', 'owner'),
  ('00000000-0000-0000-0000-00000000000b', '33333333-3333-3333-3333-333333333333', 'owner');
-- an approved fact is written by its own business's owner (kb_facts_guard, 0009), fixtures included
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values
  ('00000000-0000-0000-0000-00000000000a', 'hours', 'A', true);
select set_config('request.jwt.claim.sub', '33333333-3333-3333-3333-333333333333', true);
insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values
  ('00000000-0000-0000-0000-00000000000b', 'hours', 'B', true);
select set_config('request.jwt.claim.sub', '', true);

-- v1.7 helpers (run as superuser): a worker's tenant now comes ONLY from a live task lease
create function pg_temp.bind_as(c uuid) returns void language plpgsql as $$
declare t uuid := gen_random_uuid(); tok uuid := gen_random_uuid();
begin
  insert into app.tasks (id, public_ref, customer_id, agent_id, kind, idempotency_key)
  values (t, 't_' || substr(replace(t::text, '-', ''), 1, 12), c, 'agent_content', 'test', 'bind-' || t);
  insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
  values (t, c, 'agent_content', tok, 'test', nextval('app.fencing_seq'), now() + interval '1 hour');
  perform set_config('app.task_id', t::text, true), set_config('app.task_token', tok::text, true);
end $$;
create function pg_temp.as_user(sub uuid, aal text) returns void language sql as $$
  select set_config('request.jwt.claim.sub', sub::text, true), set_config('request.jwt.claims', json_build_object('sub', sub, 'aal', aal)::text, true);
$$;

-- 1. worker scoped to A cannot read B
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  if (select count(*) from app.kb_facts) <> 1 then raise exception 'FAIL worker sees other tenants'; end if;
  raise notice 'PASS worker read isolation';
end $$;

-- 2. worker scoped to A cannot write into B
do $$ begin
  begin
    insert into app.kb_facts (customer_id, topic, fact) values ('00000000-0000-0000-0000-00000000000b', 'x', 'y');
    raise exception 'FAIL cross-tenant insert succeeded';
  exception when insufficient_privilege or check_violation then raise notice 'PASS worker write isolation';
  end;
end $$;

-- 3. worker with no tenant set sees no tenant rows
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
do $$ begin
  if (select count(*) from app.kb_facts) <> 0 then raise exception 'FAIL unscoped worker sees tenant rows'; end if;
  raise notice 'PASS unscoped worker';
end $$;
reset role;

-- 4. owner of A sees only A
set local role authenticated;
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
do $$ begin
  if (select count(*) from app.kb_facts) <> 1 then raise exception 'FAIL owner sees other tenants'; end if;
  raise notice 'PASS owner isolation';
end $$;
reset role;

-- 5. publish without approval is rejected
do $$ begin
  begin
    insert into app.content_items (customer_id, week_id, kind, body, status)
    values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'x', 'published');
    raise exception 'FAIL published without approval';
  exception when check_violation or raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
    raise notice 'PASS publish guard';
  end;
end $$;

-- 6. audit log is append-only
insert into app.audit_log (actor_type, actor_id, action) values ('system', 'test', 'test.event');
do $$ begin
  begin
    update app.audit_log set action = 'tampered';
    raise exception 'FAIL audit update succeeded';
  exception when raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
    raise notice 'PASS audit append-only';
  end;
end $$;
-- 7. outbox refuses an external effect without an approval
do $$ begin
  begin
    insert into app.outbox (customer_id, topic, payload)
    values ('00000000-0000-0000-0000-00000000000a', 'reply.send', '{}');
    raise exception 'FAIL outbox accepted reply.send without approval';
  exception when check_violation or raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
    raise notice 'PASS outbox approval check';
  end;
end $$;

-- 8. an unsigned webhook event can be stored but never marked processed; duplicates are rejected
insert into app.webhook_events (kind, external_event_id, signature_valid, payload)
values ('whatsapp_cloud', 'wamid.TEST1', false, '{}');
do $$ begin
  begin
    update app.webhook_events set processed_at = now() where external_event_id = 'wamid.TEST1';
    raise exception 'FAIL unsigned event processed';
  exception when check_violation then raise notice 'PASS unsigned event not processable';
  end;
  begin
    insert into app.webhook_events (kind, external_event_id, signature_valid, payload)
    values ('whatsapp_cloud', 'wamid.TEST1', true, '{}');
    raise exception 'FAIL duplicate webhook accepted';
  exception when unique_violation then raise notice 'PASS webhook idempotency';
  end;
end $$;

-- 9. the dispatcher hands out one task per claim with a lease; the lease, not the worker, decides the tenant
insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key, priority) values
  ('t_a1', '00000000-0000-0000-0000-00000000000a', 'agent_content', 'draft', 'k-a1', 3),
  ('t_b1', '00000000-0000-0000-0000-00000000000b', 'agent_content', 'draft', 'k-b1', 1);
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
set local role hermes_worker;
do $$ declare r record; c uuid; begin
  select * into r from app.claim_task('agent_content', 'w1', 300);
  if r.customer_id is distinct from '00000000-0000-0000-0000-00000000000a' then raise exception 'FAIL wrong first task'; end if;
  c := app.bind_task(r.task_id, r.token);
  if c is distinct from '00000000-0000-0000-0000-00000000000a' or app.worker_customer_id() is distinct from c then
    raise exception 'FAIL binding did not set the tenant from the lease';
  end if;
  if (select count(*) from app.kb_facts) <> 1 then raise exception 'FAIL bound worker sees other tenants'; end if;
  raise notice 'PASS dispatcher lease binds the tenant';
end $$;
reset role;

-- 10. security definer helpers are not callable by the worker role
set local role hermes_worker;
do $$ begin
  begin
    perform app.current_user_customer_ids();
    raise exception 'FAIL worker executed current_user_customer_ids';
  exception when insufficient_privilege then raise notice 'PASS definer helper closed to worker';
  end;
  begin
    perform app.is_operator();
    raise exception 'FAIL worker executed is_operator';
  exception when insufficient_privilege then raise notice 'PASS is_operator closed to worker';
  end;
end $$;
reset role;

-- 11. the definer helper returns only the caller's own tenants
set local role authenticated;
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
do $$ begin
  if exists (select 1 from app.current_user_customer_ids() x where x <> '00000000-0000-0000-0000-00000000000a') then
    raise exception 'FAIL helper leaked another tenant';
  end if;
  if (select app.is_operator()) then raise exception 'FAIL owner reported as operator'; end if;
  begin
    perform * from app.audit_verify();
    raise exception 'FAIL owner executed audit_verify';
  exception when insufficient_privilege then raise notice 'PASS helper scope and audit_verify closed';
  end;
end $$;
reset role;

-- 12. a lead name read from Google Maps content cannot be stored without a non-Google source
do $$ begin
  begin
    insert into app.leads (business_name, city, sector, source_url, verified_at)
    values ('x', 'city_1', 'restaurant', 'https://example.org', now());
    raise exception 'FAIL lead stored without name_source';
  exception when check_violation then raise notice 'PASS lead provenance required';
  end;
end $$;
-- 13. budget: check and reserve are one step; a reservation that would cross the agent cap is refused (R1)
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare t uuid; r text; begin
  select id into t from app.tasks where public_ref = 't_a1';
  r := app.reserve_budget('agent_content', '2026-10', t, 0.30, 0.40, 1.00, 0.40, 1.13);
  if r <> 'OK' then raise exception 'FAIL first reservation: %', r; end if;
  perform app.settle_budget('agent_content', '2026-10', t, 0.30, 0.30, true);
  r := app.reserve_budget('agent_content', '2026-10', t, 0.08, 0.40, 1.00, 0.40, 1.13);
  if r <> 'OK' then raise exception 'FAIL second reservation: %', r; end if;
  r := app.reserve_budget('agent_content', '2026-10', t, 0.03, 0.40, 1.00, 0.40, 1.13);   -- 0.30 + 0.08 reserved + 0.03 > 0.40
  if r <> 'BUDGET_EXCEEDED_AGENT' then raise exception 'FAIL reservation crossed the cap: %', r; end if;
  raise notice 'PASS atomic budget reservation';
end $$;

-- 14. idempotency: claim once, duplicate sees IN_PROGRESS, failed key re-claimable, finished key returns its result (R2)
do $$ declare k text := 'agent_content|content:draft|llm.generate|cust_aaaa|2026-W40|h1'; r jsonb; begin
  if app.claim_idempotency(k, 'agent_content') is not null then raise exception 'FAIL first claim'; end if;
  r := app.claim_idempotency(k, 'agent_content');
  if r->>'status' is distinct from 'IN_PROGRESS' then raise exception 'FAIL duplicate not held: %', r; end if;
  perform app.release_idempotency(k);
  if app.claim_idempotency(k, 'agent_content') is not null then raise exception 'FAIL failed key not re-claimable'; end if;
  perform app.complete_idempotency(k, '{"status":"OK","result":"x"}');
  r := app.claim_idempotency(k, 'agent_content');
  if r->>'result' is distinct from 'x' then raise exception 'FAIL finished key did not return its result'; end if;
  raise notice 'PASS idempotency claim';
end $$;

-- 15. a half-open circuit admits one probe (R5)
update app.agent_state set circuit = 'open', circuit_opened_at = now() - interval '1 hour', probe_in_flight = false
 where agent_id = 'agent_content' and month_id = '2026-10';
do $$ begin
  if app.claim_probe('agent_content', '2026-10', 900) <> 'PROBE' then raise exception 'FAIL no probe after cooldown'; end if;
  if app.claim_probe('agent_content', '2026-10', 900) <> 'OPEN' then raise exception 'FAIL second probe admitted'; end if;
  if app.record_outcome('agent_content', '2026-10', true, true, 3) <> 'closed' then raise exception 'FAIL probe success did not close'; end if;
  raise notice 'PASS single probe';
end $$;

-- 16. a key without agent|action|tool scope is rejected (R3)
do $$ begin
  begin
    perform app.claim_idempotency('cust_aaaa|2026-W40|h1', 'agent_content');
    raise exception 'FAIL unscoped key accepted';
  exception when check_violation then raise notice 'PASS key scope enforced';
  end;
end $$;
reset role;
-- 17. a dead key holder is taken over only after its lease; a terminal failure is never re-claimed (N2, N3)
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare k text := 'agent_replies|reply:draft|reply.draft|cust_aaaa|m9'; r jsonb; begin
  if app.claim_idempotency(k, 'agent_replies', 60) is not null then raise exception 'FAIL first claim'; end if;
  r := app.claim_idempotency(k, 'agent_replies', 60);
  if r->>'status' is distinct from 'IN_PROGRESS' then raise exception 'FAIL live lease not respected'; end if;
  update app.idempotency_keys set lease_until = now() - interval '1 second' where key = k;   -- holder died
  if app.claim_idempotency(k, 'agent_replies', 60) is not null then raise exception 'FAIL expired lease not taken over'; end if;
  perform app.fail_idempotency(k, true);                                                     -- terminal
  r := app.claim_idempotency(k, 'agent_replies', 60);
  if r->>'status' is distinct from 'FAILED' then raise exception 'FAIL terminal failure re-claimed: %', r; end if;
  raise notice 'PASS key lease and terminal state';
end $$;

-- 18. a probe whose holder died expires; a live probe blocks (N1)
update app.agent_state set circuit = 'open', circuit_opened_at = now() - interval '1 hour', probe_in_flight = true,
       probe_until = now() + interval '1 minute'
 where agent_id = 'agent_content' and month_id = '2026-10';
do $$ begin
  if app.claim_probe('agent_content', '2026-10', 900, 660) <> 'OPEN' then raise exception 'FAIL live probe ignored'; end if;
  update app.agent_state set probe_until = now() - interval '1 second' where agent_id = 'agent_content' and month_id = '2026-10';
  if app.claim_probe('agent_content', '2026-10', 900, 660) <> 'PROBE' then raise exception 'FAIL dead probe not replaced'; end if;
  raise notice 'PASS probe lease';
end $$;

-- 19. a second overshoot within 24 h pauses the agent, and reserve_budget then refuses (N5)
do $$ declare t uuid; begin
  select id into t from app.tasks where public_ref = 't_a1';
  if app.record_reservation_exceeded('agent_content', 0.03, 0.05) then raise exception 'FAIL paused after one overshoot'; end if;
  if not app.record_reservation_exceeded('agent_content', 0.03, 0.05) then raise exception 'FAIL not paused after two'; end if;
  if app.reserve_budget('agent_content', '2026-10', t, 0.01, 0.40, 1.00, 0.40, 1.13) <> 'AGENT_PAUSED' then
    raise exception 'FAIL paused agent reserved budget';
  end if;
  raise notice 'PASS pause on repeated overshoot';
end $$;
reset role;
-- 20. production deploy without an approved approval is rejected (claim C4.2)
do $$ declare site uuid; begin
  insert into app.templates (code, version, min_schema, max_schema) values ('restaurant', '1.0.0', 1, 1) on conflict do nothing;
  insert into app.sites (id, customer_id, template_code, pinned_version) values ('00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-00000000000a', 'restaurant', '1.0.0') returning id into site;
  begin
    insert into app.deployments (customer_id, site_id, template_version, env, content_hash)
    values ('00000000-0000-0000-0000-00000000000a', site, '1.0.0', 'prod', 'h');
    raise exception 'FAIL prod deploy without approval';
  exception when check_violation or raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
    raise notice 'PASS prod deploy guard';
  end;
end $$;

-- 21. activation without a matched payment is rejected; a matched payment needs a verifier (C4.3, C4.6)
do $$ declare sub uuid; inv uuid; begin
  insert into app.subscriptions (customer_id, price_usd, period_start, period_end) values ('00000000-0000-0000-0000-00000000000a', 200, '2026-10-01', '2027-10-01') returning id into sub;
  insert into app.invoices (customer_id, subscription_id, number, amount, currency) values ('00000000-0000-0000-0000-00000000000a', sub, 'INV-T1', 200, 'USD') returning id into inv;
  begin
    update app.subscriptions set status = 'active' where id = sub;
    raise exception 'FAIL activated without payment';
  exception when raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
  end;
  begin
    insert into app.payments (customer_id, invoice_id, channel, receipt_ref, amount, currency, received_at, status)
    values ('00000000-0000-0000-0000-00000000000a', inv, 'bank', 'R-T1', 200, 'USD', now(), 'matched');
    raise exception 'FAIL matched payment without verifier';
  exception when check_violation then raise notice 'PASS activation and verification guards';
  end;
end $$;

-- 22. a third active competitor is rejected (C4.4)
do $$ begin
  insert into app.competitors (customer_id, url, label) values ('00000000-0000-0000-0000-00000000000a', 'https://a.example', 'a'),
                                                              ('00000000-0000-0000-0000-00000000000a', 'https://b.example', 'b');
  begin
    insert into app.competitors (customer_id, url, label) values ('00000000-0000-0000-0000-00000000000a', 'https://c.example', 'c');
    raise exception 'FAIL third competitor accepted';
  exception when raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
    raise notice 'PASS competitor limit';
  end;
end $$;

-- 23. audit_verify detects tampering even by a role able to bypass the trigger (C5.8)
insert into app.audit_log (actor_type, actor_id, action) values ('system', 'test', 'second.event');
alter table app.audit_log disable trigger audit_no_update;
update app.audit_log set action = 'tampered' where action = 'second.event';
alter table app.audit_log enable trigger audit_no_update;
do $$ begin
  if not exists (select 1 from app.audit_verify()) then raise exception 'FAIL tampering not detected'; end if;
  raise notice 'PASS audit chain detects tampering';
end $$;

-- 24. the per-task cap holds in the database too (C7.5)
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare t uuid; r text; begin
  insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key) values ('t_a9', '00000000-0000-0000-0000-00000000000a', 'agent_replies', 'reply', 'k-a9') returning id into t;
  r := app.reserve_budget('agent_replies', '2026-10', t, 0.02, 0.02, 0.05, 0.20, 1.13);
  r := app.reserve_budget('agent_replies', '2026-10', t, 0.02, 0.02, 0.05, 0.20, 1.13);
  r := app.reserve_budget('agent_replies', '2026-10', t, 0.02, 0.02, 0.05, 0.20, 1.13);
  if r <> 'BUDGET_EXCEEDED_TASK' then raise exception 'FAIL task cap not enforced: %', r; end if;
  raise notice 'PASS per-task cap';
end $$;
reset role;
-- 25. a worker that sets app.customer_id itself, without a lease, sees nothing (P1-04)
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
set local role hermes_worker;
select set_config('app.customer_id', '00000000-0000-0000-0000-00000000000a', true);
do $$ begin
  if (select count(*) from app.kb_facts) <> 0 then raise exception 'FAIL self-chosen tenant honoured'; end if;
  raise notice 'PASS self-chosen tenant ignored';
end $$;
reset role;

-- 26. binding with a wrong token fails (P1-04)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  begin
    perform app.bind_task(nullif(current_setting('app.task_id'), '')::uuid, gen_random_uuid());
    raise exception 'FAIL wrong token accepted';
  exception when insufficient_privilege then raise notice 'PASS wrong token refused';
  end;
end $$;
reset role;

-- 27. an unbound worker cannot reach acquisition rows (P1-04)
insert into app.leads (business_name, city, sector, source_url, verified_at, name_source)
values ('lead x', 'city_1', 'restaurant', 'https://x.example', now(), 'owner_site');
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
set local role hermes_worker;
do $$ begin
  if (select count(*) from app.leads) <> 0 then raise exception 'FAIL unbound worker sees leads'; end if;
  raise notice 'PASS acquisition needs an acquisition lease';
end $$;
reset role;

-- 28. leases: heartbeat, takeover after expiry, fencing refuses the stale worker, dead letter (P1-09)
insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key) values
  ('t_l1', '00000000-0000-0000-0000-00000000000a', 'agent_competitor', 'check', 'k-l1');
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
set local role hermes_worker;
do $$ declare r1 record; r2 record; begin
  select * into r1 from app.claim_task('agent_competitor', 'w1', 60);
  if not app.extend_task_lease(r1.task_id, r1.token, 60) then raise exception 'FAIL heartbeat'; end if;
  perform set_config('test.task', r1.task_id::text, true), set_config('test.tok1', r1.token::text, true), set_config('test.f1', r1.fencing::text, true);
end $$;
reset role;
update app.task_leases set lease_until = now() - interval '1 second' where task_id = current_setting('test.task')::uuid;   -- w1 died
set local role hermes_worker;
do $$ declare r2 record; begin
  select * into r2 from app.claim_task('agent_competitor', 'w2', 60);
  if r2.task_id <> current_setting('test.task')::uuid then raise exception 'FAIL expired lease not recovered'; end if;
  if app.complete_task(r2.task_id, current_setting('test.tok1')::uuid, current_setting('test.f1')::bigint, 'succeeded') then
    raise exception 'FAIL stale worker completed the task';
  end if;
  if not app.complete_task(r2.task_id, r2.token, r2.fencing, 'succeeded') then raise exception 'FAIL current holder refused'; end if;
  raise notice 'PASS lease, takeover and fencing';
end $$;
reset role;
insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key, status, attempts) values
  ('t_l2', '00000000-0000-0000-0000-00000000000a', 'agent_competitor', 'check', 'k-l2', 'running', 3);
insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
select id, customer_id, agent_id, gen_random_uuid(), 'dead', nextval('app.fencing_seq'), now() - interval '1 minute' from app.tasks where public_ref = 't_l2';
set local role hermes_worker;
do $$ begin perform * from app.claim_task('agent_competitor', 'w3', 60); end $$;
reset role;
do $$ begin
  if (select status::text || '/' || error_code from app.tasks where public_ref = 't_l2') <> 'failed/DEAD_LETTER' then
    raise exception 'FAIL exhausted task not dead-lettered';
  end if;
  raise notice 'PASS dead letter';
end $$;

-- 29. the worker can only propose: no decision on insert, no update at all (P1-01)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  begin
    insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id, decision)
    values ('00000000-0000-0000-0000-00000000000a', 'content:publish', '{}', 'agent_content', 'x', 'approved');
    raise exception 'FAIL worker wrote a decision';
  exception when insufficient_privilege then null;
  end;
  insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id)
  values ('00000000-0000-0000-0000-00000000000a', 'content:publish', '{"k":1}', 'agent_content', 'x');
  begin
    update app.approvals set decision = 'approved' where target_id = 'x';
    raise exception 'FAIL worker approved its own proposal';
  exception when insufficient_privilege then raise notice 'PASS worker proposes only';
  end;
end $$;
reset role;

-- 30. operator authority needs an aal2 session; operators cannot approve for a business (P1-05, P1-01)
insert into app.operators (auth_user_id, display_name, role, mfa_enrolled, active)
values ('22222222-2222-2222-2222-222222222222', 'founder', 'founder', true, true);
select pg_temp.bind_as(null);
set local role hermes_worker;
insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id)
values (null, 'platform', 'message:send', '{"lead":"x"}', 'agent_search', 'lead-x');
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
select pg_temp.as_user('22222222-2222-2222-2222-222222222222', 'aal1');
set local role authenticated;
do $$ begin
  if app.is_operator() then raise exception 'FAIL aal1 session treated as operator'; end if;
  update app.approvals set decision = 'approved', decided_by = '22222222-2222-2222-2222-222222222222' where target_id = 'lead-x';
end $$;
reset role;
select pg_temp.as_user('22222222-2222-2222-2222-222222222222', 'aal2');
set local role authenticated;
do $$ begin
  if not app.is_operator() then raise exception 'FAIL aal2 operator refused'; end if;
  update app.approvals set decision = 'approved', decided_by = '22222222-2222-2222-2222-222222222222' where target_id = 'x';
  update app.approvals set decision = 'approved', decided_by = '22222222-2222-2222-2222-222222222222' where target_id = 'lead-x';
end $$;
reset role;
do $$ begin
  if (select decision::text from app.approvals where target_id = 'x') <> 'pending' then raise exception 'FAIL operator approved for a business'; end if;
  if (select decision::text from app.approvals where target_id = 'lead-x') <> 'approved' then raise exception 'FAIL aal2 platform approval failed'; end if;
  raise notice 'PASS session aal and approval scopes';
end $$;

-- 31. outbox verifies and consumes a matching approval; the payload freezes (P1-02); 32 in the same block
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
insert into app.content_items (id, customer_id, week_id, kind, body, platform)
values ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-00000000000a', '2026-W41', 'post', 'نص معتمد', 'facebook');
insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id)
select '00000000-0000-0000-0000-00000000000a', 'content:publish', app.content_payload(c), 'agent_content', c.id::text
from app.content_items c where c.id = '00000000-0000-0000-0000-0000000000f1';
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
update app.approvals set decision = 'approved', decided_by = '11111111-1111-1111-1111-111111111111'
 where target_id = '00000000-0000-0000-0000-0000000000f1';
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare ap uuid; pay jsonb; begin
  select id, payload into ap, pay from app.approvals where target_id = '00000000-0000-0000-0000-0000000000f1';
  begin
    insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
    values ('00000000-0000-0000-0000-00000000000a', 'content.publish', pay || '{"body":"نص آخر"}', ap, '00000000-0000-0000-0000-0000000000f1');
    raise exception 'FAIL changed payload accepted';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  begin
    insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
    values ('00000000-0000-0000-0000-00000000000a', 'reply.send', pay, ap, '00000000-0000-0000-0000-0000000000f1');
    raise exception 'FAIL wrong topic accepted';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
  values ('00000000-0000-0000-0000-00000000000a', 'content.publish', pay, ap, '00000000-0000-0000-0000-0000000000f1');
  begin
    insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
    values ('00000000-0000-0000-0000-00000000000a', 'content.publish', pay, ap, '00000000-0000-0000-0000-0000000000f1');
    raise exception 'FAIL approval reused';
  exception when raise_exception or unique_violation then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  begin
    update app.outbox set payload = pay || '{"body":"x"}' where approval_id = ap;
    raise exception 'FAIL outbox payload changed after approval';
  -- since 0010 the worker holds no UPDATE on payload at all (O2), so the grant refuses before the trigger
  exception when raise_exception or insufficient_privilege then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  raise notice 'PASS outbox verifies, consumes once and freezes the payload';
-- 32. published content is bound to its consumed approval and immutable (P1-03)
  update app.content_items set status = 'published', approval_id = ap where id = '00000000-0000-0000-0000-0000000000f1';
  begin
    update app.content_items set body = 'تعديل بعد النشر' where id = '00000000-0000-0000-0000-0000000000f1';
    raise exception 'FAIL published content changed';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
    raise notice 'PASS published content bound and immutable';
  end;
end $$;
reset role;

-- 33. prod deploy covers exactly the approved artifact; rollback restores an approved one, by an aal2 operator (P1-03)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id)
values ('00000000-0000-0000-0000-00000000000a', 'site:deploy_prod',
        jsonb_build_object('site_id', '00000000-0000-0000-0000-0000000000e1'::uuid, 'content_hash', 'h2', 'template_version', '1.0.0'),
        'agent_site_builder', '00000000-0000-0000-0000-0000000000e1');
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
update app.approvals set decision = 'approved', decided_by = '11111111-1111-1111-1111-111111111111' where target_id = '00000000-0000-0000-0000-0000000000e1';
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare ap uuid; pay jsonb; d uuid; begin
  select id, payload into ap, pay from app.approvals where target_id = '00000000-0000-0000-0000-0000000000e1';
  insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
  values ('00000000-0000-0000-0000-00000000000a', 'site.deploy_prod', pay, ap, '00000000-0000-0000-0000-0000000000e1');
  begin
    insert into app.deployments (customer_id, site_id, template_version, env, content_hash, approval_id)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '1.0.0', 'prod', 'h3', ap);
    raise exception 'FAIL deploy of an unapproved artifact';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  insert into app.deployments (id, customer_id, site_id, template_version, env, content_hash, approval_id, status)
  values ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '1.0.0', 'prod', 'h2', ap, 'deployed');
  begin
    insert into app.deployments (customer_id, site_id, template_version, env, content_hash, rollback_of)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '1.0.0', 'prod', 'h2', '00000000-0000-0000-0000-0000000000d1');
    raise exception 'FAIL worker rolled back';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
select pg_temp.as_user('22222222-2222-2222-2222-222222222222', 'aal2');
set local role authenticated;
do $$ begin
  begin
    insert into app.deployments (customer_id, site_id, template_version, env, content_hash, rollback_of)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '1.0.0', 'prod', 'h9', '00000000-0000-0000-0000-0000000000d1');
    raise exception 'FAIL rollback to a different artifact';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  insert into app.deployments (customer_id, site_id, template_version, env, content_hash, rollback_of)
  values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e1', '1.0.0', 'prod', 'h2', '00000000-0000-0000-0000-0000000000d1');
  raise notice 'PASS deploy bound to its artifact; rollback restores an approved one';
end $$;
reset role;

-- 34. only an owner marks a fact approved; any edit resets it (P1-01)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare k uuid; begin
  begin
    insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values ('00000000-0000-0000-0000-00000000000a', 'hours', 'y', true);
    raise exception 'FAIL worker approved a fact';
  exception when insufficient_privilege then null;
  end;
  -- the worker is granted no id column (0009): the database picks the id
  insert into app.kb_facts (customer_id, topic, fact) values ('00000000-0000-0000-0000-00000000000a', 'hours', '9-5') returning id into k;
  perform set_config('test.kb', k::text, true);
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
update app.kb_facts set approved_by_owner = true where id = current_setting('test.kb')::uuid;
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
update app.kb_facts set fact = '9-6' where id = current_setting('test.kb')::uuid;
reset role;
do $$ begin
  if (select approved_by_owner from app.kb_facts where id = current_setting('test.kb')::uuid) then raise exception 'FAIL edited fact kept its approval'; end if;
  raise notice 'PASS fact approval is owner-only and edit-sensitive';
end $$;

-- 35. the audit verdict does not depend on the session time zone
do $$ declare a bigint; b bigint; c bigint; begin
  perform set_config('timezone', 'UTC', true);                 select broken_at into a from app.audit_verify();
  perform set_config('timezone', 'Asia/Aden', true);           select broken_at into b from app.audit_verify();
  perform set_config('timezone', 'America/Los_Angeles', true); select broken_at into c from app.audit_verify();
  if a is distinct from b or b is distinct from c then raise exception 'FAIL audit verdict depends on time zone'; end if;
  raise notice 'PASS audit chain is time-zone independent';
end $$;

-- 36. a link from one tenant's row to another tenant's parent is impossible (P1-04 composite keys)
insert into app.sites (id, customer_id, template_code, pinned_version) values
  ('00000000-0000-0000-0000-0000000000e2', '00000000-0000-0000-0000-00000000000b', 'restaurant', '1.0.0');
do $$ begin
  begin
    insert into app.deployments (customer_id, site_id, template_version, env, content_hash)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000e2', '1.0.0', 'staging', 'h');
    raise exception 'FAIL cross-tenant parent accepted';
  exception when foreign_key_violation then raise notice 'PASS composite ownership keys';
  end;
end $$;

-- 37. a reply proposal must be decidable inside the WhatsApp service window
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  begin
    insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id, expires_at)
    values ('00000000-0000-0000-0000-00000000000a', 'reply:send', '{}', 'agent_replies', 'm1', now() + interval '2 days');
    raise exception 'FAIL reply proposal outlives the service window';
  exception when check_violation then raise notice 'PASS reply window';
  end;
end $$;
reset role;
-- ===================================================================== v1.8 (0010): closure evidence for the 1.7 review
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);

-- 38. a lease that expires during a transaction stops granting its tenant at once (L1: clock time, not transaction start)
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
do $$ declare t uuid := gen_random_uuid(); tok uuid := gen_random_uuid(); begin
  insert into app.tasks (id, public_ref, customer_id, agent_id, kind, idempotency_key, status)
  values (t, 't_short1', '00000000-0000-0000-0000-00000000000a', 'agent_content', 'test', 'k-short1', 'running');
  insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
  values (t, '00000000-0000-0000-0000-00000000000a', 'agent_content', tok, 'short', nextval('app.fencing_seq'), clock_timestamp() + interval '1 second');
  perform set_config('app.task_id', t::text, true), set_config('app.task_token', tok::text, true);
end $$;
set local role hermes_worker;
do $$ begin
  if (select count(*) from app.kb_facts) = 0 then raise exception 'FAIL live short lease refused'; end if;
  perform pg_sleep(1.3);
  if (select count(*) from app.kb_facts) <> 0 then raise exception 'FAIL expired lease still grants the tenant inside the transaction'; end if;
  raise notice 'PASS lease expiry takes effect mid-transaction';
end $$;
reset role;

-- 39. an expired or recovered holder can neither complete, heartbeat, re-queue nor bind; tasks change only through lease functions (L2, T1)
insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key) values
  ('t_r1', '00000000-0000-0000-0000-00000000000a', 'agent_site_builder', 'build', 'k-r1');
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
set local role hermes_worker;
do $$ declare r1 record; begin
  select * into r1 from app.claim_task('agent_site_builder', 'w1', 60);
  perform set_config('test.r_task', r1.task_id::text, true), set_config('test.r_tok', r1.token::text, true),
          set_config('test.r_f', r1.fencing::text, true);
end $$;
reset role;
update app.task_leases set lease_until = clock_timestamp() - interval '1 second' where task_id = current_setting('test.r_task')::uuid;
set local role hermes_worker;
do $$ declare r2 record; t uuid := current_setting('test.r_task')::uuid; tok1 uuid := current_setting('test.r_tok')::uuid;
              f1 bigint := current_setting('test.r_f')::bigint; begin
  if app.complete_task(t, tok1, f1, 'succeeded') then raise exception 'FAIL expired holder completed before recovery'; end if;
  select * into r2 from app.claim_task('agent_site_builder', 'w2', 60);
  if r2.task_id is distinct from t then raise exception 'FAIL expired task not recovered'; end if;
  if app.extend_task_lease(t, tok1, 60) then raise exception 'FAIL stale heartbeat accepted'; end if;
  if app.requeue_task(t, tok1, 5) then raise exception 'FAIL stale holder re-queued the task'; end if;
  begin
    perform app.bind_task(t, tok1);
    raise exception 'FAIL stale token bound';
  exception when insufficient_privilege then null;
  end;
  perform app.bind_task(t, r2.token);
  begin
    update app.tasks set attempts = 0 where id = t;
    raise exception 'FAIL worker updated a task row directly';
  exception when insufficient_privilege then null;
  end;
  begin
    perform app.complete_task(t, r2.token, r2.fencing, 'running');
    raise exception 'FAIL non-terminal completion accepted';
  exception when invalid_parameter_value then null;
  end;
  if not app.complete_task(t, r2.token, r2.fencing, 'succeeded') then raise exception 'FAIL current holder refused'; end if;
  if app.complete_task(t, r2.token, r2.fencing, 'succeeded') then raise exception 'FAIL task completed twice'; end if;
  raise notice 'PASS stale holder refused everywhere; tasks change only through lease functions';
end $$;
reset role;

-- 40. a redelivered request returns the existing outbox row; another payload for a consumed approval is refused (O3)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
insert into app.content_items (id, customer_id, week_id, kind, body, platform)
values ('00000000-0000-0000-0000-0000000000f5', '00000000-0000-0000-0000-00000000000a', '2026-W42', 'post', 'منشور ثانٍ', 'facebook');
insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id)
select '00000000-0000-0000-0000-00000000000a', 'content:publish', app.content_payload(c), 'agent_content', c.id::text
  from app.content_items c where c.id = '00000000-0000-0000-0000-0000000000f5';
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
update app.approvals set decision = 'approved', decided_by = '11111111-1111-1111-1111-111111111111'
 where target_id = '00000000-0000-0000-0000-0000000000f5';
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare ap uuid; pay jsonb; id1 bigint; id2 bigint; begin
  select id, payload into ap, pay from app.approvals where target_id = '00000000-0000-0000-0000-0000000000f5';
  id1 := app.enqueue_outbox('content.publish', pay, ap, '00000000-0000-0000-0000-0000000000f5');
  id2 := app.enqueue_outbox('content.publish', pay, ap, '00000000-0000-0000-0000-0000000000f5');
  if id1 is distinct from id2 then raise exception 'FAIL redelivery created a second effect'; end if;
  if (select count(*) from app.outbox where approval_id = ap) <> 1 then raise exception 'FAIL more than one outbox row'; end if;
  begin
    perform app.enqueue_outbox('content.publish', pay || '{"body":"x"}', ap, '00000000-0000-0000-0000-0000000000f5');
    raise exception 'FAIL consumed approval reused for another payload';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  perform set_config('test.ob', id1::text, true);
  raise notice 'PASS redelivery returns the existing outbox row';
end $$;

-- 41. dispatch: an expired, unconfirmed send is never re-claimed; only an aal2 operator clears the human check (O1, O2)
do $$ declare ob bigint := current_setting('test.ob')::bigint; tok uuid; begin
  tok := app.claim_outbox_dispatch(ob, 60);
  if tok is null then raise exception 'FAIL first claim refused'; end if;
  if app.claim_outbox_dispatch(ob, 60) is not null then raise exception 'FAIL live claim handed out twice'; end if;
  begin
    update app.outbox set sending_until = null, sending_token = null where id = ob;
    raise exception 'FAIL live lease released without BEFORE_SEND';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  perform set_config('test.ob_tok', tok::text, true);
end $$;
reset role;
alter table app.outbox disable trigger outbox_before_write;           -- simulate a dispatcher that died mid-send
update app.outbox set sending_until = clock_timestamp() - interval '1 second' where id = current_setting('test.ob')::bigint;
alter table app.outbox enable trigger outbox_before_write;
set local role hermes_worker;
do $$ declare ob bigint := current_setting('test.ob')::bigint; begin
  if app.claim_outbox_dispatch(ob, 60) is not null then raise exception 'FAIL expired unconfirmed send re-claimed'; end if;
  if not (select needs_human_check from app.outbox where id = ob) then raise exception 'FAIL ambiguous send not flagged'; end if;
  begin
    update app.outbox set needs_human_check = false where id = ob;
    raise exception 'FAIL worker cleared the human check';
  exception when insufficient_privilege then null;
  end;
  begin
    perform app.finish_outbox_dispatch(ob, current_setting('test.ob_tok')::uuid, 'sent', null);
    raise exception 'FAIL result recorded over a human check';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal2');
set local role authenticated;
do $$ begin
  begin
    perform app.resolve_outbox(current_setting('test.ob')::bigint, 'resend', 'owner tries to resend');
    raise exception 'FAIL a non-operator resolved an ambiguous send';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
select pg_temp.as_user('22222222-2222-2222-2222-222222222222', 'aal2');
set local role authenticated;
select app.resolve_outbox(current_setting('test.ob')::bigint, 'confirmed_sent', 'checked the page: the post is live');
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);
do $$ declare o app.outbox; begin
  select * into o from app.outbox where id = current_setting('test.ob')::bigint;
  if o.dispatched_at is null or o.needs_human_check or o.resolved_by is distinct from '22222222-2222-2222-2222-222222222222' then
    raise exception 'FAIL operator resolution not recorded';
  end if;
  begin
    update app.outbox set failed_at = now(), last_error = 'x' where id = o.id;
    raise exception 'FAIL a final outcome changed';
  exception when raise_exception then if sqlerrm like 'FAIL%' then raise; end if;
  end;
  raise notice 'PASS ambiguous send waits for an aal2 operator; outcomes are final';
end $$;

-- 42. webhook ingest: the tenant comes from the addressed channel identity, never from the caller; the worker sets processed_at only (W1)
insert into app.channel_accounts (customer_id, kind, external_id, status, verified_at)
values ('00000000-0000-0000-0000-00000000000a', 'whatsapp_cloud', 'pn-A', 'active', now());
set local role hermes_ingest;
insert into app.webhook_events (kind, external_event_id, signature_valid, payload, channel_external_id)
values ('whatsapp_cloud', 'wamid.R1', true,  '{}', 'pn-A'),
       ('whatsapp_cloud', 'wamid.R2', true,  '{}', 'pn-unknown'),
       ('whatsapp_cloud', 'wamid.R3', false, '{}', 'pn-A');
do $$ begin
  begin
    insert into app.webhook_events (kind, external_event_id, signature_valid, payload, customer_id)
    values ('whatsapp_cloud', 'wamid.R4', true, '{}', '00000000-0000-0000-0000-00000000000b');
    raise exception 'FAIL ingest chose a tenant';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
do $$ begin
  if (select customer_id from app.webhook_events where external_event_id = 'wamid.R1') is distinct from '00000000-0000-0000-0000-00000000000a' then
    raise exception 'FAIL signed event not routed';
  end if;
  if (select customer_id from app.webhook_events where external_event_id = 'wamid.R2') is not null then raise exception 'FAIL unknown channel routed'; end if;
  if (select customer_id from app.webhook_events where external_event_id = 'wamid.R3') is not null then raise exception 'FAIL unsigned event routed'; end if;
  if (select count(*) from app.tasks where idempotency_key like 'wh:whatsapp_cloud:wamid.R%') <> 1 then
    raise exception 'FAIL inbound tasks: expected exactly one (the routed, signed event)';
  end if;
end $$;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  update app.webhook_events set processed_at = now() where external_event_id = 'wamid.R1';
  begin
    update app.webhook_events set signature_valid = true where external_event_id = 'wamid.R1';
    raise exception 'FAIL worker rewrote the signature flag';
  exception when insufficient_privilege then null;
  end;
  raise notice 'PASS webhook routed by channel identity; worker sets processed_at only';
end $$;
reset role;

-- 43. effects are audited inside their own transaction, with the real actor; audit rows cannot impersonate (A1; review A15, first half)
do $$ begin
  if not exists (select 1 from app.audit_log where action = 'outbox.enqueued' and target = 'outbox:' || current_setting('test.ob')
                 and actor_type = 'agent' and actor_id = 'agent_content') then raise exception 'FAIL enqueue not audited as the leased agent'; end if;
  if not exists (select 1 from app.audit_log where action = 'outbox.resolved' and actor_type = 'operator'
                 and actor_id = '22222222-2222-2222-2222-222222222222') then raise exception 'FAIL operator resolution not audited'; end if;
  if not exists (select 1 from app.audit_log where action = 'approval.approved' and actor_type = 'user'
                 and actor_id = '11111111-1111-1111-1111-111111111111') then raise exception 'FAIL owner decision not audited'; end if;
end $$;
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
do $$ begin
  begin
    insert into app.audit_log (actor_type, actor_id, customer_id, action)
    values ('operator', '22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-00000000000a', 'forged');
    raise exception 'FAIL owner wrote an operator audit row';
  exception when insufficient_privilege then null;
  end;
  insert into app.audit_log (actor_type, actor_id, customer_id, action)
  values ('user', '11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-00000000000a', 'owner.note');
end $$;
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
set local role hermes_worker;
do $$ begin
  begin
    insert into app.audit_log (actor_type, actor_id, action) values ('agent', 'agent_x', 'unleased.write');
    raise exception 'FAIL unleased worker wrote an audit row';
  exception when insufficient_privilege then null;
  end;
  raise notice 'PASS effects audited in their transaction; audit rows cannot impersonate';
end $$;
reset role;

-- 44. an approval is consumed only by the outbox trigger: a session setting no longer opens the door (C1)
create role t_rogue nologin;
grant usage on schema app to t_rogue;
grant select on app.approvals to t_rogue;
grant update (consumed_at, consumed_by_ref) on app.approvals to t_rogue;
create policy approvals_rogue on app.approvals for all to t_rogue using (true) with check (true);
set local role t_rogue;
select set_config('app.consuming', 'on', true);
do $$ begin
  begin
    update app.approvals set consumed_at = now(), consumed_by_ref = 'outbox:0' where consumed_at is null;
    raise exception 'FAIL approval consumed outside the outbox';
  exception when insufficient_privilege then raise notice 'PASS consumption only through the outbox trigger';
  end;
end $$;
reset role;

-- 45. inquiry bodies are purged after 30 days by the job role, which never reads a body (C4.7)
insert into app.inquiries (customer_id, source, received_at, body) values
  ('00000000-0000-0000-0000-00000000000a', 'whatsapp', now() - interval '31 days', 'old body a'),
  ('00000000-0000-0000-0000-00000000000b', 'whatsapp', now() - interval '45 days', 'old body b'),
  ('00000000-0000-0000-0000-00000000000a', 'whatsapp', now() - interval '2 days',  'recent body');
set local role hermes_jobs;
do $$ declare n int; begin
  begin
    perform body from app.inquiries limit 1;
    raise exception 'FAIL job role read a body';
  exception when insufficient_privilege then null;
  end;
  n := app.purge_inquiry_bodies();
  if n < 2 then raise exception 'FAIL purge touched % rows', n; end if;
end $$;
reset role;
do $$ begin
  if exists (select 1 from app.inquiries where body is not null and received_at < now() - interval '30 days') then raise exception 'FAIL old body kept'; end if;
  if not exists (select 1 from app.inquiries where body = 'recent body') then raise exception 'FAIL recent body purged'; end if;
  if (select last_run_at from app.v_retention_status) is null then raise exception 'FAIL retention run not recorded'; end if;
  raise notice 'PASS retention purges old bodies without reading them';
end $$;

-- 45b. a failed purge records no run: retention_runs is written inside the purge's own transaction, so its latest row
--      is the latest SUCCESS, and a run that fails (a permission error, a timeout, a lock) cannot silence the monitor
do $$ declare before bigint; begin
  select count(*) into before from app.retention_runs;
  begin
    create function app.t45b_fail() returns trigger language plpgsql as $f$ begin raise exception 'forced purge failure'; end $f$;
    create trigger t45b_fail before update on app.inquiries for each row execute function app.t45b_fail();
    insert into app.inquiries (customer_id, source, received_at, body)
    values ('00000000-0000-0000-0000-00000000000a', 'whatsapp', now() - interval '40 days', 'body a failing purge meets');
    perform app.purge_inquiry_bodies();
    raise exception 'FAIL the forced purge failure did not happen';
  exception when raise_exception then
    if sqlerrm like 'FAIL%' then raise; end if;
  end;
  if (select count(*) from app.retention_runs) <> before then raise exception 'FAIL a failed purge recorded a run'; end if;
  if exists (select 1 from pg_proc where proname = 't45b_fail') then raise exception 'FAIL test trigger survived'; end if;
  raise notice 'PASS a failed purge records no run: the latest retention_runs row is the latest success';
end $$;

-- 49. an effect without an approval is enqueued once per target: a re-run task gets its row back, never a second
--     notice; a different payload for the same target is refused, and a missing target too (0014)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare id1 bigint; id2 bigint; pay jsonb := '{"event":"wamid.T49","reason":"complaint","categories":[],"sla_minutes":60}'; begin
  id1 := app.enqueue_outbox('notify.owner', pay, null, 'wamid.T49');
  id2 := app.enqueue_outbox('notify.owner', pay, null, 'wamid.T49');
  if id1 is distinct from id2 then raise exception 'FAIL a re-run enqueued a second owner notice (% and %)', id1, id2; end if;
  begin
    perform app.enqueue_outbox('notify.owner', pay || '{"reason":"other"}', null, 'wamid.T49');
    raise exception 'FAIL a different notice for the same target was accepted';
  exception when raise_exception then if sqlerrm <> 'OUTBOX_TARGET_CONFLICT' then raise; end if;
  end;
  begin
    perform app.enqueue_outbox('notify.owner', pay, null, null);
    raise exception 'FAIL a notice without a target was accepted';
  exception when invalid_parameter_value then if sqlerrm <> 'OUTBOX_TARGET_REQUIRED' then raise; end if;
  end;
  raise notice 'PASS an effect without an approval is enqueued once per target';
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
do $$ begin
  if (select count(*) from app.outbox where target_id = 'wamid.T49') <> 1 then raise exception 'FAIL more than one row for the target'; end if;
  if not exists (select 1 from pg_indexes where schemaname = 'app' and indexname = 'outbox_effect_once') then
    raise exception 'FAIL no unique index backs the target identity';
  end if;
  raise notice 'PASS one outbox row per target, backed by a unique index';
end $$;

-- 50. an operator settles an outbox row that waits for a human: only with aal2, only with a reason, only once; a
--     "resend" queues the task that sends it again (0015), and a platform row cannot be resent through a worker
insert into app.outbox (customer_id, topic, payload, target_id)
values ('00000000-0000-0000-0000-00000000000a', 'notify.owner', '{"event":"T50"}', 'T50'),
       (null, 'notify.owner', '{"event":"T50p"}', 'T50p');
update app.outbox set needs_human_check = true, last_error = 'AMBIGUOUS:TEST' where target_id in ('T50', 'T50p');
select pg_temp.as_user('22222222-2222-2222-2222-222222222222', 'aal1');
set local role authenticated;
do $$ declare ob bigint; begin
  begin
    perform app.resolve_outbox((select id from app.outbox where target_id = 'T50'), 'resend', 'provider says not delivered');
    raise exception 'FAIL an aal1 operator resolved a row';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
select pg_temp.as_user('22222222-2222-2222-2222-222222222222', 'aal2');
set local role authenticated;
do $$ declare ob bigint; begin
  select id into ob from app.outbox where target_id = 'T50';
  begin
    perform app.resolve_outbox(ob, 'resend', 'no');
    raise exception 'FAIL a resolution without a reason was accepted';
  exception when raise_exception then if sqlerrm <> 'REASON_REQUIRED' then raise; end if;
  end;
  perform app.resolve_outbox(ob, 'resend', 'provider log shows it was never delivered');
  if not exists (select 1 from app.tasks where idempotency_key = 'resend:' || ob || ':0' and kind = 'outbox.resend'
                 and agent_id = 'agent_triage' and status = 'queued' and customer_id = '00000000-0000-0000-0000-00000000000a') then
    raise exception 'FAIL resend queued no task';
  end if;
  if exists (select 1 from app.outbox where id = ob and (needs_human_check or sending_until is not null or resolution <> 'resend')) then
    raise exception 'FAIL the row is not pending again after resend';
  end if;
  begin
    perform app.resolve_outbox(ob, 'abandon', 'second decision on the same row');
    raise exception 'FAIL a settled row was settled again';
  exception when raise_exception then if sqlerrm <> 'OUTBOX_NOT_WAITING_FOR_HUMAN' then raise; end if;
  end;
  begin
    perform app.resolve_outbox((select id from app.outbox where target_id = 'T50p'), 'resend', 'platform row, no customer');
    raise exception 'FAIL a platform row was queued for a worker';
  exception when raise_exception then if sqlerrm <> 'OUTBOX_RESEND_NEEDS_CUSTOMER' then raise; end if;
  end;
  raise notice 'PASS an operator settles a waiting row once, with aal2 and a reason; resend queues its task';
end $$;
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);

-- 51. a worker claims only the kinds it names; no list claims every kind, as before (0016)
insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key, priority) values
  ('t_k51new', '00000000-0000-0000-0000-00000000000a', 'agent_kindtest', 'future.kind', 'k51:new', 3),
  ('t_k51old', '00000000-0000-0000-0000-00000000000a', 'agent_kindtest', 'inbound.event', 'k51:old', 0);
set local role hermes_worker;
do $$ declare r record; begin                     -- the worker cannot read tasks itself (RLS): record, compare below
  select * into r from app.claim_task('agent_kindtest', 'w51', 60, 3, array['inbound.event']);
  perform set_config('test.k51_first', coalesce(r.task_id::text, ''), true);
  select * into r from app.claim_task('agent_kindtest', 'w51', 60, 3, array['inbound.event']);
  perform set_config('test.k51_second', coalesce(r.task_id::text, ''), true);
  select * into r from app.claim_task('agent_kindtest', 'w51', 60);
  perform set_config('test.k51_any', coalesce(r.task_id::text, ''), true);
end $$;
reset role;
do $$ begin
  if current_setting('test.k51_first') is distinct from (select id::text from app.tasks where idempotency_key = 'k51:old') then
    raise exception 'FAIL a worker claimed a kind it does not name (or nothing)';
  end if;
  if current_setting('test.k51_second') <> '' then raise exception 'FAIL the unknown kind was handed out'; end if;
  if current_setting('test.k51_any') is distinct from (select id::text from app.tasks where idempotency_key = 'k51:new') then
    raise exception 'FAIL without a list every kind is claimable';
  end if;
  raise notice 'PASS a worker claims only the task kinds it names';
end $$;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);

-- 52. the competitor job (hermes_jobs): reads active competitors and their snapshots, writes a snapshot only for the
--     competitor's own customer, at most 10 per customer per calendar month, and never an inactive competitor (0017)
insert into app.competitors (id, customer_id, url, label, active) values
  ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-00000000000b', 'https://k52-a.test/', 'k52 active', true),
  ('00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-00000000000b', 'https://k52-b.test/', 'k52 retired', false);
set local role hermes_jobs;
do $$ declare n int; begin
  if not exists (select 1 from app.competitors where id = '00000000-0000-0000-0000-0000000000c1') then
    raise exception 'FAIL the job cannot see an active competitor';
  end if;
  if exists (select 1 from app.competitors where id = '00000000-0000-0000-0000-0000000000c2') then
    raise exception 'FAIL the job sees a retired competitor';
  end if;
  begin
    insert into app.competitor_snapshots (customer_id, competitor_id, status, diff_summary)
    values ('00000000-0000-0000-0000-00000000000a', '00000000-0000-0000-0000-0000000000c1', 'unverifiable', 'x');
    raise exception 'FAIL a snapshot was filed under another customer';
  exception when insufficient_privilege then null;
  end;
  begin
    insert into app.competitor_snapshots (customer_id, competitor_id, status, diff_summary)
    values ('00000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-0000000000c2', 'unverifiable', 'x');
    raise exception 'FAIL a retired competitor was checked';
  exception when insufficient_privilege then null;
  end;
  for n in 1..10 loop
    insert into app.competitor_snapshots (customer_id, competitor_id, content_hash, status, diff_summary, structured_facts, page_hash)
    values ('00000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-0000000000c1', repeat('a', 64), 'ok', 'check ' || n,
            '{"items": []}', repeat('b', 64));
  end loop;
  begin
    insert into app.competitor_snapshots (customer_id, competitor_id, status, diff_summary)
    values ('00000000-0000-0000-0000-00000000000b', '00000000-0000-0000-0000-0000000000c1', 'unverifiable', 'eleventh');
    raise exception 'FAIL an eleventh check this month was accepted';
  exception when insufficient_privilege then null;
  end;
  begin
    perform diff_summary from app.competitor_snapshots limit 1;
    raise exception 'FAIL the job reads the owner summaries';
  exception when insufficient_privilege then null;
  end;
  raise notice 'PASS the competitor job writes for the competitor''s own customer only, ten a month, never a retired one';
end $$;
reset role;

-- 53. one inquiry per inbound message: a re-run task inserts it once; another customer's same id is its own (0018)
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  insert into app.inquiries (customer_id, source, body, event_ref) values ('00000000-0000-0000-0000-00000000000a', 'whatsapp', 'k53', 'wamid.K53')
    on conflict (customer_id, event_ref) where event_ref is not null do nothing;
  insert into app.inquiries (customer_id, source, body, event_ref) values ('00000000-0000-0000-0000-00000000000a', 'whatsapp', 'k53', 'wamid.K53')
    on conflict (customer_id, event_ref) where event_ref is not null do nothing;
  if (select count(*) from app.inquiries where event_ref = 'wamid.K53') <> 1 then
    raise exception 'FAIL a re-run task inserted the same message twice';
  end if;
  begin
    insert into app.inquiries (customer_id, source, body, event_ref) values ('00000000-0000-0000-0000-00000000000a', 'whatsapp', 'k53', 'wamid.K53');
    raise exception 'FAIL a plain second insert of the same message was accepted';
  exception when unique_violation then null;
  end;
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000b');
set local role hermes_worker;
do $$ begin
  insert into app.inquiries (customer_id, source, body, event_ref) values ('00000000-0000-0000-0000-00000000000b', 'whatsapp', 'k53b', 'wamid.K53')
    on conflict (customer_id, event_ref) where event_ref is not null do nothing;
  if (select count(*) from app.inquiries where event_ref = 'wamid.K53') <> 1 then
    raise exception 'FAIL the same message id under another customer was dropped (or the first is visible)';
  end if;
  raise notice 'PASS one inquiry per inbound message per customer, backed by a unique index';
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);

-- 54. standing approval (0020): the owner grants a low-risk topic once; the database, never the worker, decides a
--     reply that carries exactly the approved fact; an edited fact, a revoked grant or a different text waits for the owner
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values ('00000000-0000-0000-0000-00000000000a', 'location', 'L54', true),
  ('00000000-0000-0000-0000-00000000000a', 'prices', 'P54', true);
select set_config('request.jwt.claim.sub', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
do $$ begin
  begin
    insert into app.standing_approvals (customer_id, topic, fact_hash, granted_by)
    values ('00000000-0000-0000-0000-00000000000a', 'location', app.fact_hash('not the approved text'), '11111111-1111-1111-1111-111111111111');
    raise exception 'FAIL a grant for a text the owner never approved was accepted';
  exception when raise_exception then if sqlerrm <> 'STANDING_FACT_NOT_APPROVED' then raise; end if;
  end;
  begin
    insert into app.standing_approvals (customer_id, topic, fact_hash, granted_by)
    values ('00000000-0000-0000-0000-00000000000b', 'location', app.fact_hash('B'), '11111111-1111-1111-1111-111111111111');
    raise exception 'FAIL an owner granted for another business';
  exception when insufficient_privilege then null;
  end;
  begin
    insert into app.standing_approvals (customer_id, topic, fact_hash, granted_by)
    values ('00000000-0000-0000-0000-00000000000a', 'prices', app.fact_hash('P54'), '11111111-1111-1111-1111-111111111111');
    raise exception 'FAIL a standing grant for prices was accepted';
  exception when check_violation then null;
  end;
  insert into app.standing_approvals (customer_id, topic, fact_hash, granted_by)
  values ('00000000-0000-0000-0000-00000000000a', 'location', app.fact_hash('L54'), '11111111-1111-1111-1111-111111111111');
end $$;
reset role;
select pg_temp.as_user('00000000-0000-0000-0000-000000000000', 'aal1'), set_config('request.jwt.claim.sub', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare ok uuid; other uuid; ok_done boolean; other_done boolean; begin
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54","in_reply_to":"w54a","topic":"location"}', 'agent_replies', 'w54a',
          now() + interval '1 hour') returning id into ok;
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54, and 20% off today","in_reply_to":"w54b","topic":"location"}',
          'agent_replies', 'w54b', now() + interval '1 hour') returning id into other;
  begin
    update app.approvals set decision = 'approved' where id = ok;
    raise exception 'FAIL the worker decided a proposal itself';
  exception when insufficient_privilege then null;
  end;
  ok_done := app.approve_by_standing(ok);
  other_done := app.approve_by_standing(other);
  perform set_config('test.k54', ok::text || ',' || other::text || ',' || ok_done || ',' || other_done, true);
end $$;
reset role;
do $$ declare v text[] := string_to_array(current_setting('test.k54'), ','); a app.approvals; b app.approvals; begin
  select * into a from app.approvals where id = v[1]::uuid;
  select * into b from app.approvals where id = v[2]::uuid;
  if v[3] <> 'true' or a.decision <> 'approved' or a.decided_via <> 'standing'
     or a.decided_by <> '11111111-1111-1111-1111-111111111111' then
    raise exception 'FAIL the exact approved fact was not decided as the granting owner: % %', v[3], row_to_json(a);
  end if;
  if v[4] <> 'false' or b.decision <> 'pending' then raise exception 'FAIL a different text went out on a standing grant'; end if;
end $$;
-- a second approved fact for the same topic does not hide the granted one (0021, found on staging)
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values ('00000000-0000-0000-0000-00000000000a', 'location', 'L54 old', true);
select set_config('request.jwt.claim.sub', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare p uuid; begin
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54","in_reply_to":"w54d","topic":"location"}', 'agent_replies', 'w54d',
          now() + interval '1 hour') returning id into p;
  if not app.approve_by_standing(p) then raise exception 'FAIL a second approved fact hid the granted one'; end if;
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54 old","in_reply_to":"w54e","topic":"location"}', 'agent_replies', 'w54e',
          now() + interval '1 hour') returning id into p;
  if app.approve_by_standing(p) then raise exception 'FAIL an approved fact the owner did not grant went out'; end if;
end $$;
reset role;
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
delete from app.kb_facts where customer_id = '00000000-0000-0000-0000-00000000000a' and fact = 'L54 old';
select set_config('request.jwt.claim.sub', '', true);
-- an edited fact is a new fact: the grant no longer matches; a revoked grant decides nothing
select set_config('request.jwt.claim.sub', '11111111-1111-1111-1111-111111111111', true);
update app.kb_facts set fact = 'L54b' where customer_id = '00000000-0000-0000-0000-00000000000a' and topic = 'location';
update app.kb_facts set approved_by_owner = true where customer_id = '00000000-0000-0000-0000-00000000000a' and topic = 'location';
select set_config('request.jwt.claim.sub', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare p uuid; begin
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54b","in_reply_to":"w54c","topic":"location"}', 'agent_replies', 'w54c',
          now() + interval '1 hour') returning id into p;
  if app.approve_by_standing(p) then raise exception 'FAIL an edited fact went out on the old grant'; end if;
end $$;
reset role;
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
do $$ begin
  update app.standing_approvals set revoked_at = now(), revoked_by = '11111111-1111-1111-1111-111111111111'
   where customer_id = '00000000-0000-0000-0000-00000000000a' and topic = 'location' and revoked_at is null;
  begin
    update app.standing_approvals set revoked_at = null, revoked_by = null where topic = 'location' and customer_id = '00000000-0000-0000-0000-00000000000a';
    raise exception 'FAIL a revoked grant was revived';
  exception when raise_exception then if sqlerrm <> 'STANDING_IMMUTABLE' then raise; end if;
  end;
  raise notice 'PASS a standing approval decides only the exact approved fact, as its owner; edited or revoked, nothing';
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('request.jwt.claim.sub', '', true),
       set_config('request.jwt.claims', '', true);

-- 55. one switch (0022): the owner pauses every instant reply of the business and resumes it; the grants stay; only
--     the business's own owner can flip it
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
do $$ begin
  insert into app.standing_approvals (customer_id, topic, fact_hash, granted_by)
  values ('00000000-0000-0000-0000-00000000000a', 'location', app.fact_hash('L54b'), '11111111-1111-1111-1111-111111111111');
  insert into app.standing_pause (customer_id, paused, changed_by)
  values ('00000000-0000-0000-0000-00000000000a', true, '11111111-1111-1111-1111-111111111111');
  begin
    insert into app.standing_pause (customer_id, paused, changed_by)
    values ('00000000-0000-0000-0000-00000000000b', true, '11111111-1111-1111-1111-111111111111');
    raise exception 'FAIL an owner paused another business';
  exception when insufficient_privilege then null;
  end;
end $$;
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare p uuid; begin
  begin
    update app.standing_pause set paused = false;
    raise exception 'FAIL the worker flipped the switch';
  exception when insufficient_privilege then null;
  end;
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54b","in_reply_to":"w55a","topic":"location"}', 'agent_replies', 'w55a',
          now() + interval '1 hour') returning id into p;
  if app.approve_by_standing(p) then raise exception 'FAIL an instant reply went out while paused'; end if;
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
update app.standing_pause set paused = false, changed_by = '11111111-1111-1111-1111-111111111111'
 where customer_id = '00000000-0000-0000-0000-00000000000a';
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ declare p uuid; begin
  insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)
  values ('00000000-0000-0000-0000-00000000000a', 'customer', 'reply:send',
          '{"phone_number_id":"pn","to":"9677","body":"L54b","in_reply_to":"w55b","topic":"location"}', 'agent_replies', 'w55b',
          now() + interval '1 hour') returning id into p;
  if not app.approve_by_standing(p) then raise exception 'FAIL resuming did not bring the grant back'; end if;
  raise notice 'PASS one switch pauses every instant reply of the business and resumes it, the owner''s alone';
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true);

-- 56. posts (0023): the owner drafts only for an active linked account of their own business; Instagram needs an image;
--     the owner queues the proposal of their own draft only, and cannot edit a draft afterwards
insert into app.channel_accounts (customer_id, kind, external_id, status, verified_at) values
  ('00000000-0000-0000-0000-00000000000a', 'facebook_page', '1069900000056', 'active', now()),
  ('00000000-0000-0000-0000-00000000000a', 'instagram_business', '1784199000056', 'active', now()),
  ('00000000-0000-0000-0000-00000000000b', 'facebook_page', '1069900000057', 'active', now()),
  ('00000000-0000-0000-0000-00000000000a', 'tiktok_business', '_000k56TikTok', 'active', now());
select pg_temp.as_user('11111111-1111-1111-1111-111111111111', 'aal1');
set local role authenticated;
do $$ declare c uuid; n int; begin
  insert into app.content_items (customer_id, week_id, kind, body, platform, account_id)
  values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'عرض الجمعة', 'facebook', '1069900000056') returning id into c;
  insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key)
  values ('t_k56a', '00000000-0000-0000-0000-00000000000a', 'agent_triage', 'content.propose', 'content:' || c);
  begin
    insert into app.content_items (customer_id, week_id, kind, body, platform, account_id)
    values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'x', 'facebook', '1069900000057');
    raise exception 'FAIL a draft for another business''s page was accepted';
  exception when raise_exception then if sqlerrm <> 'CONTENT_ACCOUNT_NOT_LINKED' then raise; end if;
  end;
  begin
    insert into app.content_items (customer_id, week_id, kind, body, platform, account_id)
    values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'x', 'instagram', '1784199000056');
    raise exception 'FAIL an Instagram post without an image was accepted';
  exception when raise_exception then if sqlerrm <> 'CONTENT_IMAGE_REQUIRED' then raise; end if;
  end;
  begin
    insert into app.content_items (customer_id, week_id, kind, body, platform, account_id)
    values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'x', 'tiktok', '_000k56TikTok');
    raise exception 'FAIL a TikTok post without an image was accepted';
  exception when raise_exception then if sqlerrm <> 'CONTENT_IMAGE_REQUIRED' then raise; end if;
  end;
  insert into app.content_items (customer_id, week_id, kind, body, platform, account_id, image_url)
  values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'x', 'tiktok', '_000k56TikTok', 'https://cdn.example/p.jpg');
  begin
    insert into app.content_items (customer_id, week_id, kind, body, platform, account_id, status)
    values ('00000000-0000-0000-0000-00000000000a', '2026-W40', 'post', 'x', 'facebook', '1069900000056', 'approved');
    raise exception 'FAIL an owner inserted an approved post';
  exception when insufficient_privilege then null;
  end;
  begin
    insert into app.tasks (public_ref, customer_id, agent_id, kind, idempotency_key)
    values ('t_k56b', '00000000-0000-0000-0000-00000000000a', 'agent_triage', 'outbox.resend', 'resend:1:0');
    raise exception 'FAIL an owner queued a task that is not their draft''s proposal';
  exception when insufficient_privilege then null;
  end;
  update app.content_items set body = 'edited after the fact' where id = c;
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'FAIL the owner edited a draft after queueing it'; end if;
  raise notice 'PASS posts: drafts only for the owner''s own linked accounts, Instagram with an image, proposal of their own draft only';
end $$;
reset role;
select set_config('request.jwt.claim.sub', '', true), set_config('request.jwt.claims', '', true);

-- 57. email (0025): an inbound address has one spelling, routes a signed event to its business like a phone number id,
--     and the worker records an email inquiry
do $$ begin
  begin
    insert into app.channel_accounts (customer_id, kind, external_id, status, verified_at)
    values ('00000000-0000-0000-0000-00000000000a', 'email', 'Shop <Shop-K57@Inbound.Example>', 'active', now());
    raise exception 'FAIL an email channel with a display name and capitals was accepted';
  exception when check_violation then null;
  end;
end $$;
insert into app.channel_accounts (customer_id, kind, external_id, status, verified_at)
values ('00000000-0000-0000-0000-00000000000a', 'email', 'shop-k57@inbound.example', 'active', now());
set local role hermes_ingest;
insert into app.webhook_events (kind, external_event_id, signature_valid, payload, channel_external_id)
values ('email', 'k57-a8c1040e-db1f', true, '{"email": {}}', 'shop-k57@inbound.example'),
       ('email', 'k57-b8c1040e-db1f', true, '{"email": {}}', 'Shop-K57@inbound.example');
reset role;
select pg_temp.bind_as('00000000-0000-0000-0000-00000000000a');
set local role hermes_worker;
do $$ begin
  insert into app.inquiries (customer_id, source, body, event_ref)
  values ('00000000-0000-0000-0000-00000000000a', 'email', 'k57', 'k57-a8c1040e-db1f');
end $$;
reset role;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
do $$ begin
  if (select customer_id from app.webhook_events where external_event_id = 'k57-a8c1040e-db1f') is distinct from '00000000-0000-0000-0000-00000000000a'
     or (select customer_id from app.webhook_events where external_event_id = 'k57-b8c1040e-db1f') is not null then
    raise exception 'FAIL an email event was not routed by its exact inbound address';
  end if;
  if (select count(*) from app.inquiries where event_ref = 'k57-a8c1040e-db1f' and source = 'email') <> 1 then
    raise exception 'FAIL the email inquiry was not recorded';
  end if;
  raise notice 'PASS email: one spelling per inbound address, routed by it exactly, recorded as an email inquiry';
end $$;

-- 46. the FINAL catalog after all migrations matches the published inventory (grants and policies accumulate)
do $$ declare got text; bad text; begin
  select string_agg(relname, ',' order by relname collate "C") into got from pg_class
   where relnamespace = 'app'::regnamespace and relkind = 'r' and relrowsecurity and not relforcerowsecurity;
  if got is distinct from 'approvals,audit_log,customer_users,operators,outbox_topics,task_leases,tasks' then
    raise exception 'FAIL FORCE exceptions differ from docs/security_definer_inventory.md: %', got;
  end if;
  select string_agg(relname, ',') into bad from pg_class where relnamespace = 'app'::regnamespace and relkind = 'r' and not relrowsecurity;
  if bad is not null then raise exception 'FAIL tables without RLS: %', bad; end if;
  raise notice 'PASS FORCE inventory matches the catalog';

  select string_agg(p.proname, ',') into bad from pg_proc p
   where p.pronamespace = 'app'::regnamespace
     and (p.proacl is null or exists (select 1 from aclexplode(p.proacl) a where a.grantee = 0 and a.privilege_type = 'EXECUTE'));
  if bad is not null then raise exception 'FAIL app functions executable by PUBLIC: %', bad; end if;
  select string_agg(p.proname, ',' order by p.proname collate "C") into got from pg_proc p
   where p.pronamespace = 'app'::regnamespace and p.prosecdef;
  if got is distinct from 'approve_by_standing,audit_chain,audit_effect,audit_head,audit_verify,bind_task,claim_task,complete_task,current_user_customer_ids,extend_task_lease,health_signals,is_operator,jwt_aal,outbox_before_write,requeue_task,worker_context,worker_customer_id' then raise exception 'FAIL definer functions differ from the inventory: %', got; end if;
  select string_agg(p.proname, ',') into bad from pg_proc p where p.pronamespace = 'app'::regnamespace and p.prosecdef
     and not exists (select 1 from unnest(coalesce(p.proconfig, '{}'::text[])) c where c like 'search_path=%');
  if bad is not null then raise exception 'FAIL definer functions without a pinned search_path: %', bad; end if;
  raise notice 'PASS function privileges and definer inventory match the catalog';

  select string_agg(format('%s:%s:%s', r.rolname, c.relname, p.pr), ',') into bad
    from pg_class c
    cross join (values ('hermes_worker'), ('authenticated'), ('hermes_ingest'), ('hermes_jobs'), ('hermes_monitor'),
                      ('hermes_monitor_reader')) r(rolname)
    cross join (values ('DELETE'), ('TRUNCATE')) p(pr)
   where c.relnamespace = 'app'::regnamespace and c.relkind = 'r' and has_table_privilege(r.rolname, c.oid, p.pr);
  if bad is not null then raise exception 'FAIL delete/truncate held by an application role: %', bad; end if;
  if has_table_privilege('hermes_worker', 'app.tasks', 'UPDATE') or has_table_privilege('hermes_worker', 'app.tasks', 'INSERT')
     or has_table_privilege('hermes_worker', 'app.approvals', 'UPDATE') or has_table_privilege('hermes_worker', 'app.approvals', 'INSERT')
     or has_table_privilege('hermes_worker', 'app.outbox', 'UPDATE') or has_table_privilege('hermes_worker', 'app.kb_facts', 'UPDATE')
     or has_table_privilege('hermes_worker', 'app.webhook_events', 'UPDATE') or has_table_privilege('hermes_worker', 'app.webhook_events', 'INSERT')
     or has_any_column_privilege('hermes_worker', 'app.task_leases', 'SELECT')
     or has_table_privilege('authenticated', 'app.approvals', 'UPDATE') or has_table_privilege('authenticated', 'app.approvals', 'INSERT')
     or has_column_privilege('authenticated', 'app.approvals', 'consumed_at', 'UPDATE')
     or has_column_privilege('hermes_worker', 'app.outbox', 'resolution', 'UPDATE')
     or has_column_privilege('hermes_worker', 'app.kb_facts', 'approved_by_owner', 'UPDATE')
     or has_column_privilege('hermes_ingest', 'app.webhook_events', 'customer_id', 'INSERT')
     or has_column_privilege('hermes_jobs', 'app.inquiries', 'body', 'SELECT') then
    raise exception 'FAIL a broad privilege survived the column-level narrowing';
  end if;
  select string_agg(rolname, ',') into bad from pg_roles
   where rolname in ('hermes_worker', 'hermes_ingest', 'hermes_jobs', 'hermes_monitor', 'hermes_monitor_reader', 'authenticated') and (rolsuper or rolbypassrls);
  if bad is not null then raise exception 'FAIL application role with superuser or bypassrls: %', bad; end if;
  select string_agg(r.rolname || '->' || g.rolname, ',') into bad from pg_auth_members m
    join pg_roles r on r.oid = m.member join pg_roles g on g.oid = m.roleid
   where r.rolname in ('hermes_worker', 'hermes_ingest', 'hermes_jobs', 'hermes_monitor', 'hermes_monitor_reader', 'authenticated');
  if bad is not null then raise exception 'FAIL application role inherits another role: %', bad; end if;
  raise notice 'PASS no broad or inherited privilege survives';

  select string_agg(tablename || '.' || policyname, ',') into bad from pg_policies where schemaname = 'app' and 'public' = any(roles);
  if bad is not null then raise exception 'FAIL policy granted to PUBLIC: %', bad; end if;
  select string_agg(tablename || '.' || policyname, ',' order by tablename) into bad from pg_policies
   where schemaname = 'app' and 'hermes_worker' = any(roles) and permissive = 'PERMISSIVE'
     and policyname not in ('templates_read', 'outbox_topics_read', 'agent_pauses_worker_read')
     and coalesce(qual, '') || coalesce(with_check, '') !~ 'worker_(customer_id|context)';
  if bad is not null then raise exception 'FAIL worker policy not bound to a lease: %', bad; end if;
  raise notice 'PASS every worker policy is bound to a lease';
end $$;
-- 47. the external monitor gets numbers, never rows: its role holds EXECUTE on one function and nothing else, and
--     the function's owner reads timestamps and flags only (0011)
do $$ declare bad text; begin
  select string_agg(c.relname, ',') into bad from pg_class c
   where c.relnamespace = 'app'::regnamespace and c.relkind in ('r', 'v', 'm')
     and (has_table_privilege('hermes_monitor', c.oid, 'SELECT') or has_any_column_privilege('hermes_monitor', c.oid, 'SELECT'));
  if bad is not null then raise exception 'FAIL monitor role can read relations: %', bad; end if;
  select string_agg(p.proname, ',') into bad from pg_proc p
   where p.pronamespace = 'app'::regnamespace and p.proname <> 'health_signals' and has_function_privilege('hermes_monitor', p.oid, 'EXECUTE');
  if bad is not null then raise exception 'FAIL monitor role executes more than health_signals: %', bad; end if;
  if (select pg_get_userbyid(proowner) from pg_proc where oid = 'app.health_signals()'::regprocedure) <> 'hermes_monitor_reader' then
    raise exception 'FAIL health_signals is not owned by the reader role';
  end if;
  select string_agg(format('%s.%s', c.relname, a.attname), ',') into bad from pg_class c join pg_attribute a on a.attrelid = c.oid
   where c.relnamespace = 'app'::regnamespace and a.attnum > 0 and not a.attisdropped
     and has_column_privilege('hermes_monitor_reader', c.oid, a.attnum, 'SELECT')
     and format('%s.%s', c.relname, a.attname) not in ('retention_runs.job', 'retention_runs.ran_at', 'inquiries.received_at',
       'inquiries.body_purged_at', 'outbox.dispatched_at', 'outbox.failed_at', 'outbox.needs_human_check', 'outbox.sending_until',
       'webhook_events.customer_id', 'webhook_events.signature_valid', 'webhook_events.received_at', 'webhook_events.processed_at',
       'monitor_epoch.started_at');
  if bad is not null then raise exception 'FAIL reader role reads columns beyond timestamps and flags: %', bad; end if;
  select string_agg(format('%s:%s', c.relname, p.pr), ',') into bad from pg_class c
   cross join (values ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE')) p(pr)
   where c.relnamespace = 'app'::regnamespace and c.relkind = 'r' and has_table_privilege('hermes_monitor_reader', c.oid, p.pr);
  if bad is not null then raise exception 'FAIL reader role can write: %', bad; end if;
  if has_schema_privilege('hermes_monitor_reader', 'app', 'CREATE') then raise exception 'FAIL reader role kept CREATE on app'; end if;
  select string_agg(rolname, ',') into bad from pg_roles where rolname in ('hermes_monitor', 'hermes_monitor_reader') and rolcanlogin;
  if bad is not null then raise exception 'FAIL monitor roles can log in: %', bad; end if;
  raise notice 'PASS the monitor role holds one function; its owner reads timestamps and flags only';
end $$;
set local role hermes_monitor;
do $$ declare s record; begin
  begin
    perform 1 from app.outbox limit 1;
    raise exception 'FAIL monitor role read a table';
  exception when insufficient_privilege then null;
  end;
  select * into s from app.health_signals();
  if s.retention_age_seconds is null or s.retention_age_seconds > 60 then raise exception 'FAIL retention age % after case 45', s.retention_age_seconds; end if;
  if s.overdue_bodies <> 0 then raise exception 'FAIL overdue bodies after the purge: %', s.overdue_bodies; end if;
  if s.outbox_attention is null or s.webhook_backlog is null or s.webhook_unrouted is null then raise exception 'FAIL null signal'; end if;
  if s.retention_stale or s.retention_due_at < now() + interval '25 hours' then
    raise exception 'FAIL just purged, yet stale=% due=%', s.retention_stale, s.retention_due_at;
  end if;
end $$;
reset role;
-- a purge that never ran is due 26 h after monitoring began (0012), not at once; past that it is stale
do $$ declare s record; begin
  begin
    delete from app.retention_runs;
    select * into s from app.health_signals();
    if s.retention_age_seconds is not null or s.retention_stale then raise exception 'FAIL never ran, stale before due: %', s; end if;
    update app.monitor_epoch set started_at = now() - interval '27 hours';
    select * into s from app.health_signals();
    if not s.retention_stale then raise exception 'FAIL never ran and 27 h since monitoring began, yet not stale'; end if;
    update app.monitor_epoch set started_at = now();
    insert into app.retention_runs (job, rows_affected, ran_at) values ('inquiry_body_30d', 0, now() - interval '27 hours');
    select * into s from app.health_signals();
    if not s.retention_stale then raise exception 'FAIL last success 27 h ago, yet not stale'; end if;
    raise exception 'ROLLBACK 47b';
  exception when raise_exception then
    if sqlerrm <> 'ROLLBACK 47b' then raise; end if;
  end;
  raise notice 'PASS retention is due 26 h after the last success, or after monitoring began when it never ran';
end $$;
do $$ begin
  if (select count(*) from app.v_outbox_attention) <> (select outbox_attention from app.health_signals()) then
    raise exception 'FAIL outbox_attention differs from v_outbox_attention';
  end if;
  raise notice 'PASS the monitor gets numbers from one function, agreeing with the operator view, and cannot read a row';
end $$;
-- 48. every table whose policies filter by customer_id has an index leading with customer_id (0013)
do $$ declare bad text; begin
  select string_agg(distinct p.tablename, ',') into bad from pg_policies p
    join pg_class c on c.relname = p.tablename and c.relnamespace = 'app'::regnamespace
    join pg_attribute a on a.attrelid = c.oid and a.attname = 'customer_id'
   where p.schemaname = 'app' and coalesce(p.qual, '') || coalesce(p.with_check, '') ~ '\mcustomer_id\M'
     and not exists (select 1 from pg_index i where i.indrelid = c.oid and i.indkey[0] = a.attnum);
  if bad is not null then raise exception 'FAIL tables filtered by customer_id without a leading index: %', bad; end if;
  raise notice 'PASS every table filtered by customer_id has an index leading with it';
end $$;

do $$ declare o text; begin
  select format('owner=%s superuser=%s bypassrls=%s', r.rolname, r.rolsuper, r.rolbypassrls) into o
    from pg_class c join pg_roles r on r.oid = c.relowner where c.oid = 'app.tasks'::regclass;
  raise notice 'INFO table owner: % (with superuser or bypassrls the FORCE exceptions are moot; run_isolation.sh uses a plain owner)', o;
end $$;
select set_config('app.task_id', '', true), set_config('app.task_token', '', true), set_config('app.customer_id', '', true);
set local role hermes_worker;
do $$ declare r record; n bigint; bad text := ''; begin
  for r in select c.relname from pg_class c
            where c.relnamespace = 'app'::regnamespace and c.relkind in ('r', 'v')
              and has_table_privilege('hermes_worker', c.oid, 'SELECT')
              and c.relname not in ('templates', 'outbox_topics', 'agent_pauses') loop
    execute format('select count(*) from app.%I', r.relname) into n;
    if n > 0 then bad := bad || r.relname || '=' || n || ' '; end if;
  end loop;
  if bad <> '' then raise exception 'FAIL an unleased worker sees rows in: %', bad; end if;
  raise notice 'PASS an unleased worker sees nothing in any table';
end $$;
reset role;

rollback;
