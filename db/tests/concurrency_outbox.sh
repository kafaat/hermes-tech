#!/usr/bin/env bash
# Two real sessions deliver the SAME approved effect at once (a redelivered event). NOT executed in the build environment.
# Expected: both get the same outbox id back; exactly one outbox row; the approval consumed once (review of 1.7, §4).
set -euo pipefail
cd "$(dirname "$0")/../.."
C=00000000-0000-0000-0000-0000000000dd; U=33333333-3333-3333-3333-333333333333; I=00000000-0000-0000-0000-0000000000d1
T=00000000-0000-0000-0000-0000000000d9; K=00000000-0000-0000-0000-0000000001d9
psql -q -v ON_ERROR_STOP=1 <<SQL
insert into app.customers (id, public_ref, business_name, sector, city, currency_zone, status)
values ('$C', 'cust_dddd', 'race outbox', 'restaurant', 'city_1', 'zone_a', 'active') on conflict do nothing;
insert into app.customer_users (customer_id, auth_user_id, role) values ('$C', '$U', 'owner') on conflict do nothing;
insert into app.content_items (id, customer_id, week_id, kind, body, platform)
values ('$I', '$C', '2026-W43', 'post', 'سباق', 'facebook') on conflict do nothing;
insert into app.approvals (customer_id, proposal_action, payload, requested_by_agent, target_id)
select '$C', 'content:publish', app.content_payload(c), 'agent_content', c.id::text from app.content_items c where c.id = '$I';
begin;
select set_config('request.jwt.claim.sub', '$U', true), set_config('request.jwt.claims', '{"sub":"$U","aal":"aal1"}', true);
set local role authenticated;
update app.approvals set decision = 'approved', decided_by = '$U' where target_id = '$I';
commit;
insert into app.tasks (id, public_ref, customer_id, agent_id, kind, idempotency_key, status)
values ('$T', 't_d9', '$C', 'agent_content', 'publish', 'race-outbox', 'running') on conflict do nothing;
insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
values ('$T', '$C', 'agent_content', '$K', 'race', nextval('app.fencing_seq'), now() + interval '10 minutes')
on conflict (task_id) do update set token = excluded.token, lease_until = excluded.lease_until;
SQL
deliver() {  # $1 delay, $2 hold
  psql -qtA -v ON_ERROR_STOP=1 <<SQL
begin; set local role hermes_worker; select app.bind_task('$T', '$K');
select pg_sleep($1);
select 'ID=' || app.enqueue_outbox('content.publish', a.payload, a.id, '$I') from app.approvals a where a.target_id = '$I';
select pg_sleep($2); commit;
SQL
}
deliver 0 2 > /tmp/oa.out &
deliver 0.5 0 > /tmp/ob.out &
wait
a=$(grep -o 'ID=[0-9]*' /tmp/oa.out); b=$(grep -o 'ID=[0-9]*' /tmp/ob.out)
n=$(psql -qtA -c "select count(*) from app.outbox o join app.approvals a on a.id = o.approval_id where a.target_id = '$I'")
echo "A=$a B=$b rows=$n"
[ -n "$a" ] && [ "$a" = "$b" ] && [ "$n" -eq 1 ] || { echo "concurrency_outbox: FAIL"; exit 1; }
echo "concurrency_outbox: PASS (one effect, both deliveries answered with it)"
