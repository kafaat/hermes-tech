#!/usr/bin/env python3
"""What k6 cannot see (docs/load_targets.md): every accepted event routed, one task per event, duplicates absorbed,
and the queue drained, with the arrival-to-decision time (inbound_to_triage) and the drain time after the load.

    DATABASE_URL=... python ops/load/verify_load.py RUN_ID K6_SUMMARY_JSON [DRAIN_LIMIT_SECONDS]

Read-only: prints each result. (app.gate_measurements takes gate ids in the spec's numbering, e.g. 0.1; which gate a
load run feeds is the owner's call, so nothing is written here.) Exit 1 if any criterion fails.
"""
import json, sys, time

import psycopg

run, summary_path = sys.argv[1], sys.argv[2]
drain_limit = int(sys.argv[3]) if len(sys.argv) > 3 else 600
s = json.load(open(summary_path))["metrics"]
sent = int(s["http_reqs"]["values"]["count"])
p95_ms = s["http_req_duration"]["values"]["p(95)"]
failed = s["http_req_failed"]["values"]["rate"]
like = f"wamid.LOAD.{run}.%"
load_end = time.time()

print(f"verify: k6 sent {sent}, edge p95 {p95_ms:.4g} ms, failed rate {failed:.4g}; waiting for the queue", flush=True)
with psycopg.connect(__import__("os").environ["DATABASE_URL"], autocommit=True, connect_timeout=10,
                     options="-c statement_timeout=30000") as c:
    q = lambda sql, *a: c.execute(sql, a).fetchone()
    said = 0.0
    while True:                                   # wait for the worker to finish every task of this run
        open_tasks = q("select count(*) from app.tasks where idempotency_key like %s and status in ('queued','running')",
                       "wh:whatsapp_cloud:" + like)[0]
        if open_tasks == 0 or time.time() - load_end > drain_limit:
            break
        if time.time() - said > 30:
            print(f"verify: {open_tasks} tasks open after {time.time() - load_end:.0f} s", flush=True)
            said = time.time()
        time.sleep(2)
    drain_s = time.time() - load_end
    events, routed = q("select count(*), count(customer_id) from app.webhook_events where external_event_id like %s", like)
    tasks, done = q("select count(*), count(*) filter (where status in ('succeeded','escalated')) from app.tasks"
                    " where idempotency_key like %s", "wh:whatsapp_cloud:" + like)
    p95_triage = q("select percentile_cont(0.95) within group (order by extract(epoch from t.finished_at - e.received_at))"
                   " from app.webhook_events e join app.tasks t on t.idempotency_key = 'wh:whatsapp_cloud:' || e.external_event_id"
                   " where e.external_event_id like %s and t.finished_at is not null", like)[0]
    results = {
        "load_edge_p95_ms": (p95_ms, p95_ms < 300),
        "load_edge_error_rate": (failed, failed < 0.001),
        # k6 repeats one id in 50: exactly that many deliveries must be absorbed, and each stored event has one task
        "load_duplicates_absorbed": (sent - events, abs((sent - events) - sent // 50) <= 1 and sent >= 50 and tasks == events),
        "load_events_routed": (routed, routed == events),
        "load_tasks_completed": (done, done == tasks == events),
        "inbound_to_triage_p95_seconds": (float(p95_triage or 0), p95_triage is not None and p95_triage < 60),
        "queue_drain_seconds_after_load": (drain_s, open_tasks == 0),
    }
    # the owner notices this run sent through the outbox (one per escalated event): the evidence for interrupted sends
    ob = q("select count(*), count(*) filter (where dispatched_at is not null), count(*) filter (where sending_until is not null"
           " and dispatched_at is null and failed_at is null), count(*) filter (where needs_human_check),"
           " count(*) filter (where dispatched_at is null and failed_at is null and sending_until < now())"
           " from app.outbox where topic = 'notify.owner' and target_id like %s", like)
    print(f"outbox notify.owner: rows {ob[0]}, sent {ob[1]}, still sending {ob[2]}, needs human {ob[3]}, "
          f"sending expired {ob[4]}", flush=True)
    for gate, (value, ok) in results.items():
        print(("PASS " if ok else "FAIL ") + f"{gate} = {value:.4g}")
    print(f"load: sent {sent}, stored {events}, tasks {tasks}")
    ok = all(v[1] for v in results.values())
    print("load:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
