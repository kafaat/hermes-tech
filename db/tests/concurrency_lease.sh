#!/usr/bin/env bash
# A transaction that began under a live lease continues after the lease expires and a second worker
# recovers the task. NOT executed in the build environment. Expected: A's write after expiry is refused by
# row security, A's completion returns false, B holds the task (review of 1.7, §4: expiry inside a transaction).
set -euo pipefail
cd "$(dirname "$0")/../.."
C=00000000-0000-0000-0000-0000000000ee; T=00000000-0000-0000-0000-0000000000e1; K=00000000-0000-0000-0000-0000000001e1
psql -q -v ON_ERROR_STOP=1 <<SQL
insert into app.customers (id, public_ref, business_name, sector, city, currency_zone, status)
values ('$C', 'cust_eeee', 'race lease', 'restaurant', 'city_1', 'zone_a', 'active') on conflict do nothing;
insert into app.tasks (id, public_ref, customer_id, agent_id, kind, idempotency_key, status, attempts)
values ('$T', 't_e1', '$C', 'agent_competitor', 'check', 'race-lease', 'running', 1) on conflict do nothing;
insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
values ('$T', '$C', 'agent_competitor', '$K', 'A', nextval('app.fencing_seq'), clock_timestamp() + interval '2 seconds')
on conflict (task_id) do update set token = excluded.token, lease_until = excluded.lease_until;
SQL
session_a() {
  psql -qtA -v ON_ERROR_STOP=1 <<SQL
begin; set local role hermes_worker; select app.bind_task('$T', '$K');
select 'BEFORE=' || count(*) from app.customers;
select pg_sleep(3.5);
select 'AFTER=' || count(*) from app.customers;
insert into app.kb_facts (customer_id, topic, fact) values ('$C', 'late', 'written after the lease expired');
commit;
SQL
}
session_b() {
  sleep 2.5
  psql -qtA -v ON_ERROR_STOP=1 -c "set role hermes_worker; select 'CLAIMED=' || task_id from app.claim_task('agent_competitor', 'B', 60);"
}
set +e
session_a > /tmp/la.out 2>&1 & pa=$!
session_b > /tmp/lb.out 2>&1 & pb=$!
wait $pa; a_rc=$?; wait $pb; b_rc=$?
set -e
cat /tmp/la.out /tmp/lb.out
grep -q '^BEFORE=1$' /tmp/la.out || { echo "concurrency_lease: FAIL (A had no tenant while its lease was live)"; exit 1; }
grep -q '^AFTER=0$'  /tmp/la.out || { echo "concurrency_lease: FAIL (A kept its tenant after the lease expired)"; exit 1; }
[ "$a_rc" -ne 0 ] && grep -q 'row-level security' /tmp/la.out || { echo "concurrency_lease: FAIL (A wrote after expiry)"; exit 1; }
[ "$b_rc" -eq 0 ] && grep -q "^CLAIMED=$T$" /tmp/lb.out || { echo "concurrency_lease: FAIL (B did not recover the task)"; exit 1; }
done=$(psql -qtA -c "set role hermes_worker; select 'DONE=' || app.complete_task('$T', '$K', 1, 'succeeded')")
grep -qx 'DONE=false' <<<"$done" || { echo "concurrency_lease: FAIL (stale holder completed)"; exit 1; }
echo "concurrency_lease: PASS (expiry ends the tenant mid-transaction; recovery takes the task; stale completion refused)"
