-- 0022 · one switch: the owner pauses every instant reply of the business, and resumes, without losing the grants
-- Asked by the owner after 0020 (spec 28.20): a holiday, a crisis, a change of hours not yet entered. Pausing keeps
-- each topic's standing approval as it is; while paused, app.approve_by_standing decides nothing for that business
-- and every reply waits for the owner, as before 0020. Resuming needs no new grants. The switch is the owner's
-- alone, recorded with who and when; the database, not the portal, applies it.
begin;

create table app.standing_pause (
  customer_id uuid primary key references app.customers(id),
  paused      boolean not null,
  changed_by  uuid not null,
  changed_at  timestamptz not null default now()
);
alter table app.standing_pause enable row level security;
alter table app.standing_pause force row level security;
create policy standing_pause_owner_select on app.standing_pause for select to authenticated
  using (customer_id in (select app.current_user_customer_ids()));
create policy standing_pause_owner_insert on app.standing_pause for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()));
create policy standing_pause_owner_update on app.standing_pause for update to authenticated
  using (customer_id in (select app.current_user_customer_ids())) with check (customer_id in (select app.current_user_customer_ids()));
create policy standing_pause_operator_select on app.standing_pause for select to authenticated using ((select app.is_operator()));
create policy standing_pause_definer_read on app.standing_pause for select to hermes_standing using (paused);
grant select on app.standing_pause to authenticated;
grant insert (customer_id, paused, changed_by) on app.standing_pause to authenticated;
grant update (paused, changed_by) on app.standing_pause to authenticated;
grant select (customer_id, paused) on app.standing_pause to hermes_standing;

create function app.standing_pause_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if new.changed_by is distinct from auth.uid()
     or not exists (select 1 from app.customer_users u where u.customer_id = new.customer_id and u.auth_user_id = auth.uid() and u.active) then
    raise exception 'STANDING_OWNER_ONLY' using errcode = '42501';
  end if;
  if tg_op = 'UPDATE' and new.customer_id is distinct from old.customer_id then
    raise exception 'STANDING_IMMUTABLE' using errcode = 'P0001';
  end if;
  new.changed_at := now();
  return new;
end $$;
create trigger standing_pause_guard before insert or update on app.standing_pause
  for each row execute function app.standing_pause_guard();

grant hermes_standing to current_user;                 -- to replace a function owned by hermes_standing; ends below
create or replace function app.approve_by_standing(p_approval uuid) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare c uuid; a record; g uuid;
begin
  c := app.worker_customer_id();                       -- the lease decides the tenant, never an argument
  if c is null then return false; end if;
  if exists (select 1 from app.standing_pause where customer_id = c and paused) then
    return false;                                      -- the owner paused every instant reply (0022)
  end if;
  select id, payload, expires_at into a from app.approvals
   where id = p_approval and customer_id = c and proposal_action = 'reply:send' and decision = 'pending' for update;
  if not found or a.expires_at <= now() then return false; end if;
  select s.granted_by into g
    from app.standing_approvals s
    join app.kb_facts f on f.customer_id = s.customer_id and f.topic = s.topic
   where s.customer_id = c and s.topic = a.payload->>'topic' and s.revoked_at is null
     and f.approved_by_owner and (f.valid_until is null or f.valid_until >= current_date)
     and f.fact = a.payload->>'body' and app.fact_hash(f.fact) = s.fact_hash
   limit 1;
  if g is null then return false; end if;
  perform set_config('app.standing_approval', a.id::text, true);
  update app.approvals set decision = 'approved', decided_by = g where id = a.id;
  perform set_config('app.standing_approval', '', true);
  return true;
end $$;
do $$ begin execute format('revoke hermes_standing from %I', current_user); end $$;

commit;
