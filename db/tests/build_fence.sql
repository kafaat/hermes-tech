-- Disposable PostgreSQL regression, run as database administrator after migrations.
-- All fixtures and activations roll back.
\set ON_ERROR_STOP on
begin;
select app.activate_service_build(repeat('a',40), coalesce((select max(generation) from app.service_build),0)+1);
select generation as old_generation from app.service_build where id=1 \gset
select :old_generation + 1 as new_generation \gset
select app.activate_service_build(repeat('b',40), :new_generation);
set local role hermes_worker;
do $$ begin
  if app.heartbeat_service_build(repeat('a',40), (select generation-1 from app.service_build)) then
    raise exception 'stale restart/heartbeat regained authority';
  end if;
  begin
    update app.service_build set commit=repeat('a',40);
    raise exception 'worker changed activation';
  exception when insufficient_privilege then null; end;
  begin
    perform app.activate_service_build(repeat('a',40), 999999);
    raise exception 'worker activated generation';
  exception when insufficient_privilege then null; end;
  begin
    perform * from app.claim_task('agent_content','legacy',60);
    raise exception 'legacy claim bypass';
  exception when insufficient_privilege then null; end;
  raise notice 'PASS worker cannot change authority or bypass claim gate';
end $$;
reset role;
-- Stale liveness never restores the previous build.
update app.service_build set seen_at=clock_timestamp()-interval '1 day';
insert into app.customers(id,public_ref,business_name,sector,city,currency_zone,status)
values('00000000-0000-0000-0000-0000000000f1','cust_fence','fence','restaurant','city_1','zone_a','active');
insert into app.tasks(public_ref,customer_id,agent_id,kind,idempotency_key)
values('t_fence','00000000-0000-0000-0000-0000000000f1','agent_content','fence','build-fence');
set local role hermes_worker;
select set_config('test.old_generation', :'old_generation', true), set_config('test.new_generation', :'new_generation', true);
do $$ declare r record; begin
  if exists(select * from app.claim_task('agent_content','old',60,3,array['fence'],repeat('a',40),current_setting('test.old_generation')::bigint)) then
    raise exception 'stale build claimed after heartbeat expiry'; end if;
  if exists(select * from app.claim_task('agent_content','unknown',60,3,array['fence'],null,null)) then
    raise exception 'null identity claimed'; end if;
  select * into r from app.claim_task('agent_content','new',60,3,array['fence'],repeat('b',40),current_setting('test.new_generation')::bigint);
  if r.task_id is null then raise exception 'active build failed to claim'; end if;
  raise notice 'PASS only active generation claims even after liveness expiry';
end $$;
reset role;
do $$ begin
  begin
    perform app.activate_service_build(repeat('a',40),(select generation from app.service_build));
    raise exception 'non-monotonic activation accepted';
  exception when invalid_parameter_value then null; end;
  begin
    perform app.activate_service_build('',(select generation+1 from app.service_build));
    raise exception 'empty identity activated';
  exception when invalid_parameter_value then null; end;
  raise notice 'PASS activation requires full identity and increasing generation';
end $$;
rollback;
