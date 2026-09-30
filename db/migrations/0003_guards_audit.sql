-- =====================================================================
-- Hermes · 0003_guards_audit.sql · invariants the application cannot bypass
--   1. Nothing is published or deployed to prod without an APPROVED approval
--      for the matching proposal action.
--   2. At most two active competitors per customer (package limit).
--   3. audit_log is append-only and hash-chained.
-- =====================================================================
begin;

-- ------------------------------------------------------------ 1. approval guard
create or replace function app.assert_approved(p_approval uuid, p_customer uuid, p_action text)
returns void language plpgsql security invoker set search_path = app, pg_temp as $$
-- invoker, not definer: the caller's tenant scope must be able to see the approval
declare a record;
begin
  select decision, customer_id, proposal_action into a from app.approvals where id = p_approval;
  if not found then raise exception 'APPROVAL_NOT_FOUND %', p_approval using errcode = 'P0001'; end if;
  if a.customer_id <> p_customer then raise exception 'APPROVAL_TENANT_MISMATCH' using errcode = 'P0001'; end if;
  if a.proposal_action <> p_action then raise exception 'APPROVAL_ACTION_MISMATCH % <> %', a.proposal_action, p_action using errcode = 'P0001'; end if;
  if a.decision <> 'approved' then raise exception 'APPROVAL_NOT_APPROVED %', a.decision using errcode = 'P0001'; end if;
end $$;

create or replace function app.guard_content_publish() returns trigger
language plpgsql set search_path = app, pg_temp as $$
begin
  if new.status = 'published' and (tg_op = 'INSERT' or old.status is distinct from 'published') then
    perform app.assert_approved(new.approval_id, new.customer_id, 'content:publish');
    new.published_at := coalesce(new.published_at, now());
  end if;
  return new;
end $$;
create trigger content_publish_guard before insert or update on app.content_items
  for each row execute function app.guard_content_publish();

create or replace function app.guard_prod_deploy() returns trigger
language plpgsql set search_path = app, pg_temp as $$
begin
  if new.env = 'prod' and new.rollback_of is null then
    perform app.assert_approved(new.approval_id, new.customer_id, 'site:deploy_prod');
  end if;
  return new;
end $$;
create trigger prod_deploy_guard before insert on app.deployments
  for each row execute function app.guard_prod_deploy();

create or replace function app.guard_subscription_activate() returns trigger
language plpgsql set search_path = app, pg_temp as $$
begin
  if new.status = 'active' and (tg_op = 'INSERT' or old.status is distinct from 'active') then
    if not exists (select 1 from app.invoices i join app.payments p on p.invoice_id = i.id
                   where i.subscription_id = new.id and p.status = 'matched') then
      raise exception 'ACTIVATION_WITHOUT_MATCHED_PAYMENT' using errcode = 'P0001';
    end if;
  end if;
  return new;
end $$;
create trigger subscription_activate_guard before insert or update on app.subscriptions
  for each row execute function app.guard_subscription_activate();

-- ------------------------------------------------------------ 2. competitors limit
create or replace function app.guard_competitor_limit() returns trigger
language plpgsql set search_path = app, pg_temp as $$
begin
  if new.active and (select count(*) from app.competitors c
                     where c.customer_id = new.customer_id and c.active and c.id <> new.id) >= 2 then
    raise exception 'COMPETITOR_LIMIT_REACHED' using errcode = 'P0001';
  end if;
  return new;
end $$;
create trigger competitor_limit_guard before insert or update on app.competitors
  for each row execute function app.guard_competitor_limit();

-- ------------------------------------------------------------ updated_at
create or replace function app.touch_updated_at() returns trigger language plpgsql as $$
begin new.updated_at := now(); return new; end $$;
create trigger customers_touch before update on app.customers for each row execute function app.touch_updated_at();

-- ------------------------------------------------------------ 3. audit log: append-only hash chain
create or replace function app.audit_chain() returns trigger
language plpgsql security definer set search_path = app, extensions, public, pg_temp as $$
-- pgcrypto lives in 'extensions' on Supabase and in 'public' on plain Postgres
declare last_hash text;
begin
  perform pg_advisory_xact_lock(hashtext('app.audit_log'));
  select hash into last_hash from app.audit_log order by id desc limit 1;
  new.prev_hash := coalesce(last_hash, 'GENESIS');
  new.hash := encode(digest(new.prev_hash || '|' || new.ts::text || '|' || new.actor_type::text || '|' ||
                            new.actor_id || '|' || coalesce(new.customer_id::text,'') || '|' ||
                            new.action || '|' || coalesce(new.target,'') || '|' || new.details::text, 'sha256'), 'hex');
  return new;
end $$;
create trigger audit_chain_before_insert before insert on app.audit_log
  for each row execute function app.audit_chain();

create or replace function app.audit_immutable() returns trigger language plpgsql as $$
begin raise exception 'AUDIT_LOG_IS_APPEND_ONLY' using errcode = 'P0001'; end $$;
create trigger audit_no_update before update or delete on app.audit_log
  for each row execute function app.audit_immutable();

alter table app.audit_log enable row level security;          -- ENABLE only: trigger reads the chain tail as owner
create policy audit_operator_select on app.audit_log for select to authenticated using ((select app.is_operator()));
create policy audit_owner_select on app.audit_log for select to authenticated using (customer_id in (select app.current_user_customer_ids()));
create policy audit_insert_authenticated on app.audit_log for insert to authenticated
  with check ((select app.is_operator()) or customer_id in (select app.current_user_customer_ids()));
create policy audit_insert_worker on app.audit_log for insert to hermes_worker
  with check (customer_id is not distinct from (select app.worker_customer_id()) and actor_type in ('agent','system'));
revoke update, delete, truncate on app.audit_log from public, authenticated, hermes_worker;
grant select, insert on app.audit_log to authenticated;
grant insert on app.audit_log to hermes_worker;

create or replace function app.audit_verify() returns table(broken_at bigint)
language plpgsql stable security definer set search_path = app, extensions, public, pg_temp as $$
declare r record; prev text := 'GENESIS'; h text;
begin
  for r in select * from app.audit_log order by id loop
    h := encode(digest(prev || '|' || r.ts::text || '|' || r.actor_type::text || '|' || r.actor_id || '|' ||
                       coalesce(r.customer_id::text,'') || '|' || r.action || '|' || coalesce(r.target,'') || '|' ||
                       r.details::text, 'sha256'), 'hex');
    if r.prev_hash <> prev or r.hash <> h then broken_at := r.id; return next; return; end if;
    prev := r.hash;
  end loop;
end $$;
revoke all on function app.audit_verify() from public;

commit;
