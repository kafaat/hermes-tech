-- Disposable DB after all migrations. Exact SQL path; rollback all fixtures.
begin;
insert into app.customers (id, public_ref, business_name, sector, city, currency_zone, status)
values ('00320000-0000-0000-0000-000000000001','cust_retry32','Retry fixture','restaurant','city_1','zone_a','active');
insert into app.operators (auth_user_id, display_name, role, mfa_enrolled)
values ('00320000-0000-0000-0000-000000000002','Retry operator','founder',true);
insert into app.tasks (id, public_ref, customer_id, agent_id, kind, idempotency_key)
values ('00320000-0000-0000-0000-000000000003','t_retry32','00320000-0000-0000-0000-000000000001','agent_triage','test','retry32');
insert into app.task_leases (task_id, customer_id, agent_id, token, worker, fencing, lease_until)
values ('00320000-0000-0000-0000-000000000003','00320000-0000-0000-0000-000000000001','agent_triage',
 '00320000-0000-0000-0000-000000000004','test',nextval('app.fencing_seq'),clock_timestamp()+interval '1 hour');
select set_config('app.task_id','00320000-0000-0000-0000-000000000003',true),
       set_config('app.task_token','00320000-0000-0000-0000-000000000004',true);
set local role hermes_worker;
select set_config('test.retry32',app.enqueue_outbox('notify.owner','{"event":"retry32"}',null,'retry32')::text,true);
do $$ declare ob bigint := current_setting('test.retry32')::bigint; tok uuid; begin
  for i in 1..5 loop
    tok := app.claim_outbox_dispatch(ob);
    if tok is null then raise exception 'FAIL early claim denied'; end if;
    perform app.finish_outbox_dispatch(ob,tok,'failed_before_send',null,'test');
  end loop;
  if app.claim_outbox_dispatch(ob,120,999) is not null then raise exception 'FAIL caller bypassed retry cap'; end if;
  if not exists(select 1 from app.outbox where id=ob and attempts=5 and needs_human_check and operator_attempt_limit=0)
  then raise exception 'FAIL exhaustion state'; end if;
  begin
    update app.outbox set operator_attempt_limit=999 where id=ob;
    raise exception 'FAIL worker modified authorization';
  exception when insufficient_privilege then null; end;
  raise notice 'PASS exhausted outbox cannot enlarge its own allowance';
end $$;
reset role;
select set_config('request.jwt.claim.sub','00320000-0000-0000-0000-000000000002',true),
       set_config('request.jwt.claims','{"sub":"00320000-0000-0000-0000-000000000002","aal":"aal2"}',true);
set local role authenticated;
do $$ declare ob bigint := current_setting('test.retry32')::bigint; begin
  perform app.resolve_outbox(ob,'resend','provider confirms no effect');
  if not exists(select 1 from app.outbox where id=ob and attempts=5 and operator_attempt_limit=6 and not needs_human_check)
  then raise exception 'FAIL explicit resend did not grant one attempt'; end if;
  if not exists(select 1 from app.tasks where idempotency_key='resend:'||ob||':5')
  then raise exception 'FAIL resend task missing'; end if;
  begin
    perform app.resolve_outbox(ob,'resend','duplicate decision must fail');
    raise exception 'FAIL duplicate decision accepted';
  exception when raise_exception then if sqlerrm <> 'OUTBOX_NOT_WAITING_FOR_HUMAN' then raise; end if; end;
  begin
    update app.outbox set operator_attempt_limit=999 where id=ob;
    raise exception 'FAIL direct allowance modification';
  exception when insufficient_privilege then null; end;
  raise notice 'PASS operator grants one attempt and duplicate authorization fails';
end $$;
reset role;
select set_config('request.jwt.claim.sub','',true),set_config('request.jwt.claims','',true);
set local role hermes_worker;
do $$ declare ob bigint := current_setting('test.retry32')::bigint; tok uuid; begin
  tok := app.claim_outbox_dispatch(ob);
  if tok is null then raise exception 'FAIL authorized sixth attempt denied'; end if;
  perform set_config('test.retry32.oldtoken',tok::text,true);
  perform app.finish_outbox_dispatch(ob,tok,'failed_before_send',null,'test');
  if app.claim_outbox_dispatch(ob) is not null then raise exception 'FAIL authorization reused'; end if;
  if not exists(select 1 from app.outbox where id=ob and attempts=6 and needs_human_check)
  then raise exception 'FAIL lifetime count lost'; end if;
  raise notice 'PASS operator allowance consumed once with lifetime attempts preserved';
end $$;
reset role;
select set_config('request.jwt.claim.sub','00320000-0000-0000-0000-000000000002',true),
       set_config('request.jwt.claims','{"sub":"00320000-0000-0000-0000-000000000002","aal":"aal2"}',true);
set local role authenticated;
select app.resolve_outbox(current_setting('test.retry32')::bigint,'resend','fresh explicit retry decision');
reset role;
select set_config('request.jwt.claim.sub','',true),set_config('request.jwt.claims','',true);
set local role hermes_worker;
do $$ declare ob bigint := current_setting('test.retry32')::bigint; tok uuid; begin
  tok := app.claim_outbox_dispatch(ob);
  if tok is null then raise exception 'FAIL fresh authorization denied'; end if;
  if app.finish_outbox_dispatch(ob,current_setting('test.retry32.oldtoken')::uuid,'sent','old.receipt')
  then raise exception 'FAIL stale claim completed new retry'; end if;
  perform app.finish_outbox_dispatch(ob,tok,'ambiguous',null,'lost response');
  if app.claim_outbox_dispatch(ob) is not null then raise exception 'FAIL unknown effect retried'; end if;
  raise notice 'PASS stale tokens and unknown effects remain fenced';
end $$;
reset role;
-- A caller may intentionally lower the automatic retry cap to three.
set local role hermes_worker;
select set_config('test.retry32low',app.enqueue_outbox('notify.owner','{"event":"retry32low"}',null,'retry32low')::text,true);
do $$ declare ob bigint := current_setting('test.retry32low')::bigint; tok uuid; begin
  for i in 1..3 loop
    tok := app.claim_outbox_dispatch(ob,120,3);
    if tok is null then raise exception 'FAIL lower-cap early claim denied'; end if;
    perform app.finish_outbox_dispatch(ob,tok,'failed_before_send',null,'test');
  end loop;
  if app.claim_outbox_dispatch(ob,120,3) is not null then raise exception 'FAIL lower cap bypassed'; end if;
end $$;
reset role;
select set_config('request.jwt.claim.sub','00320000-0000-0000-0000-000000000002',true),
       set_config('request.jwt.claims','{"sub":"00320000-0000-0000-0000-000000000002","aal":"aal2"}',true);
set local role authenticated;
select app.resolve_outbox(current_setting('test.retry32low')::bigint,'resend','explicit lower-cap retry decision');
reset role;
select set_config('request.jwt.claim.sub','',true),set_config('request.jwt.claims','',true);
set local role hermes_worker;
do $$ declare ob bigint := current_setting('test.retry32low')::bigint; tok uuid; begin
  if not exists(select 1 from app.outbox where id=ob and attempts=3 and operator_attempt_limit=4)
  then raise exception 'FAIL lower-cap operator allowance'; end if;
  tok := app.claim_outbox_dispatch(ob,120,3);
  if tok is null then raise exception 'FAIL authorized fourth attempt denied'; end if;
  perform app.finish_outbox_dispatch(ob,tok,'failed_before_send',null,'test');
  if app.claim_outbox_dispatch(ob,120,3) is not null then raise exception 'FAIL lower-cap allowance reused'; end if;
  if not exists(select 1 from app.outbox where id=ob and attempts=4 and needs_human_check)
  then raise exception 'FAIL lower-cap lifetime count'; end if;
  raise notice 'PASS lower retry cap receives exactly one authorized attempt';
end $$;
reset role;
rollback;
