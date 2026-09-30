#!/usr/bin/env bash
# Two real sessions race for the last 0.03 of headroom (0.37 spent, cap 0.40). NOT executed in the build environment.
# Session A reserves and holds its transaction open for 2 s; session B tries while A's row lock is held.
# Expected: exactly one OK; B returns BUDGET_EXCEEDED_AGENT after A commits. Run after run_isolation.sh on a fresh DB.
# v1.8: 0009 made the lease the only source of a worker's tenant; setting app.customer_id (as 1.7 did here) grants nothing,
# so each session now binds its own task lease.
set -euo pipefail
cd "$(dirname "$0")/../.."
C=00000000-0000-0000-0000-0000000000cc
T1=00000000-0000-0000-0000-0000000000c1; K1=00000000-0000-0000-0000-0000000001c1
T2=00000000-0000-0000-0000-0000000000c2; K2=00000000-0000-0000-0000-0000000001c2
psql -q -v ON_ERROR_STOP=1 <<SQL
insert into app.customers (id, public_ref, business_name, sector, city, currency_zone, status)
values ('$C', 'cust_cccc', 'race', 'restaurant', 'city_1', 'zone_a', 'active') on conflict do nothing;
insert into app.tasks (id, public_ref, customer_id, agent_id, kind, idempotency_key, status)
values ('$T1', 't_c1', '$C', 'agent_content', 'draft', 'race-1', 'running'),
       ('$T2', 't_c2', '$C', 'agent_content', 'draft', 'race-2', 'running') on conflict do nothing;
insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
values ('$T1', '$C', 'agent_content', '$K1', 'race-a', nextval('app.fencing_seq'), now() + interval '10 minutes'),
       ('$T2', '$C', 'agent_content', '$K2', 'race-b', nextval('app.fencing_seq'), now() + interval '10 minutes')
on conflict (task_id) do update set token = excluded.token, lease_until = excluded.lease_until;
insert into app.agent_state (customer_id, agent_id, month_id, spent_usd) values ('$C', 'agent_content', '2026-10', 0.37)
on conflict (customer_id, agent_id, month_id) do update set spent_usd = 0.37, reserved_usd = 0;
SQL
race() {  # $1 task uuid, $2 lease token, $3 delay before reserving, $4 hold seconds
  psql -qtA -v ON_ERROR_STOP=1 <<SQL
begin; set local role hermes_worker; select app.bind_task('$1', '$2');
select pg_sleep($3);
select app.reserve_budget('agent_content', '2026-10', '$1', 0.03, 0.08, 0.40, 0.40, 1.13);
select pg_sleep($4); commit;
SQL
}
race "$T1" "$K1" 0 2 > /tmp/a.out &
race "$T2" "$K2" 0.5 0 > /tmp/b.out &
wait
a=$(grep -E '^(OK|BUDGET)' /tmp/a.out); b=$(grep -E '^(OK|BUDGET)' /tmp/b.out)
echo "A=$a B=$b"
[ "$(printf '%s\n%s\n' "$a" "$b" | grep -c '^OK$')" -eq 1 ] || { echo "concurrency: FAIL"; exit 1; }
echo "concurrency: PASS (one reservation, one refusal)"
