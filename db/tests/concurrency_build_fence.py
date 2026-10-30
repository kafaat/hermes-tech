#!/usr/bin/env python3
"""Real two-session lock regression. Disposable database only; uses PG* env.
The runner is an administrator. Source transactions SET ROLE hermes_worker.
Restores the original activation row; no provider/network effects.
"""
import concurrent.futures
import time
import psycopg


def wait_blocked(admin, pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = admin.execute('select wait_event_type from pg_stat_activity where pid=%s', (pid,)).fetchone()
        if row and row[0] == 'Lock':
            return
        time.sleep(.02)
    raise AssertionError('second transaction did not wait on the authority lock')


def claim(conn, commit, generation):
    conn.execute('set local role hermes_worker')
    return conn.execute("select * from app.claim_task('agent_content','race',60,3,array['fence-race'],%s,%s)",
                        (commit, generation)).fetchall()


def main():
    with psycopg.connect('', autocommit=True) as admin, psycopg.connect('') as first, psycopg.connect('') as second:
        prior = admin.execute('select id,commit,started_at,seen_at,generation from app.service_build').fetchone()
        generation = (prior[4] if prior else 0) + 1
        admin.execute("select app.activate_service_build(%s,%s)", ('a'*40, generation))
        customer = '00000000-0000-0000-0000-0000000000f2'
        admin.execute("insert into app.customers(id,public_ref,business_name,sector,city,currency_zone,status) values(%s,'cust_fencerace','fence','restaurant','city_1','zone_a','active')", (customer,))
        task = admin.execute("insert into app.tasks(public_ref,customer_id,agent_id,kind,idempotency_key) values('t_fencerace',%s,'agent_content','fence-race','build-fence-race') returning id", (customer,)).fetchone()[0]
        try:
            # Empty authority fails closed even if the first deployment activates
            # while the no-authority claim transaction is still open.
            admin.execute('delete from app.service_build')
            assert claim(first, 'a'*40, generation) == [], 'claim without locked authority returned a task'
            second.execute('select app.activate_service_build(%s,%s)', ('a'*40, generation))
            second.commit()
            second.execute('select app.activate_service_build(%s,%s)', ('b'*40, generation+1))
            second.commit()
            first.commit()
            assert admin.execute('select status from app.tasks where id=%s', (task,)).fetchone()[0] == 'queued'
            print('PASS empty authority claim stays empty across first activation')
            # Restore initial authority for the two lock-order regressions below.
            admin.execute('delete from app.service_build')
            admin.execute('select app.activate_service_build(%s,%s)', ('a'*40, generation))
            # Activation wins: a concurrent old claim must wait, then reject.
            first.execute('select app.activate_service_build(%s,%s)', ('b'*40, generation+1))
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(claim, second, 'a'*40, generation)
                try:
                    wait_blocked(admin, second.info.backend_pid)
                finally:
                    first.commit()  # release waiter even if the observation failed
                assert pending.result(timeout=10) == [], 'old claim escaped activation'
            second.rollback()
            # Claim wins: activation must wait until the entire claim transaction ends.
            assert claim(first, 'b'*40, generation+1), 'active build could not claim fixture'
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(second.execute, 'select app.activate_service_build(%s,%s)', ('c'*40,generation+2))
                try:
                    wait_blocked(admin, second.info.backend_pid)
                finally:
                    first.rollback()
                pending.result(timeout=10)
            second.rollback()
            print('PASS activation and claim serialize in both orders')
        finally:
            first.rollback()
            second.rollback()
            # Test-only cleanup, outside application privileges.
            with admin.transaction():
                admin.execute('delete from app.task_leases where task_id=%s', (task,))
                admin.execute('delete from app.tasks where id=%s', (task,))
                admin.execute('delete from app.customers where id=%s', (customer,))
                admin.execute('delete from app.service_build')
                if prior:
                    admin.execute('insert into app.service_build(id,commit,started_at,seen_at,generation) values(%s,%s,%s,%s,%s)',prior)


if __name__ == '__main__':
    main()
