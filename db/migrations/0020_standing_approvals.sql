-- 0020 · standing approval: the owner approves the reply for a low-risk topic once, and it goes out at once
-- Meta's own agent answers on WhatsApp instantly (docs/positioning.md); a Hermes reply waited for the owner, up to
-- 19 hours. For hours, location and payment methods the owner may now grant a standing approval: the next question
-- on that topic is answered at once, with exactly the fact the owner approved. Prices, offers, promises and
-- complaints keep the per-message decision. Nothing reaches a customer without the owner's decision: the decision
-- is made in advance, for one topic, for one exact text, and the owner revokes it from the portal at any time.
--
-- The worker never decides. app.approve_by_standing(approval) decides a pending reply proposal only when, for the
-- worker's leased customer: a standing grant for the proposal's topic is active; exactly one approved, valid fact
-- exists for that topic; its text equals the proposal's body; and its hash equals the hash the owner granted (an
-- edited fact is a new fact: kb_facts_guard resets its approval and the hash no longer matches). The decision is
-- recorded as the granting owner's, with decided_via = 'standing'.
--
-- The function is owned by hermes_standing (NOLOGIN, like hermes_monitor_reader in 0011): the tables are FORCE'd,
-- and an owner-run definer would need owner-wide policies that open every other definer too.
begin;

do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_standing') then create role hermes_standing nologin noinherit; end if;
end $$;
grant usage on schema app to hermes_standing;

create table app.standing_approvals (
  id          uuid primary key default gen_random_uuid(),
  customer_id uuid not null references app.customers(id),
  topic       text not null check (topic in ('hours', 'location', 'payment')),
  fact_hash   text not null check (fact_hash ~ '^[0-9a-f]{64}$'),
  granted_by  uuid not null,
  granted_at  timestamptz not null default now(),
  revoked_at  timestamptz,
  revoked_by  uuid,
  check ((revoked_at is null) = (revoked_by is null))
);
create unique index standing_approvals_active on app.standing_approvals (customer_id, topic) where revoked_at is null;
create index standing_approvals_customer on app.standing_approvals (customer_id);
alter table app.standing_approvals enable row level security;
alter table app.standing_approvals force row level security;
create policy standing_owner_select on app.standing_approvals for select to authenticated
  using (customer_id in (select app.current_user_customer_ids()));
create policy standing_owner_grant on app.standing_approvals for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()));
create policy standing_owner_revoke on app.standing_approvals for update to authenticated
  using (customer_id in (select app.current_user_customer_ids())) with check (customer_id in (select app.current_user_customer_ids()));
create policy standing_operator_select on app.standing_approvals for select to authenticated using ((select app.is_operator()));
create policy standing_definer_read on app.standing_approvals for select to hermes_standing using (revoked_at is null);
grant select on app.standing_approvals to authenticated;
grant insert (customer_id, topic, fact_hash, granted_by) on app.standing_approvals to authenticated;
grant update (revoked_at, revoked_by) on app.standing_approvals to authenticated;
grant select (customer_id, topic, fact_hash, granted_by, revoked_at) on app.standing_approvals to hermes_standing;

create function app.fact_hash(t text) returns text language sql immutable set search_path = pg_catalog as $$
  select encode(sha256(convert_to(t, 'UTF8')), 'hex')
$$;
revoke execute on function app.fact_hash(text) from public;
grant execute on function app.fact_hash(text) to authenticated, hermes_standing, hermes_worker;

-- the owner grants for an approved fact of their own business, as themselves; a grant is never edited, only revoked
create function app.standing_approvals_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if not exists (select 1 from app.customer_users u where u.customer_id = new.customer_id and u.auth_user_id = auth.uid() and u.active) then
    raise exception 'STANDING_OWNER_ONLY' using errcode = '42501';
  end if;
  if tg_op = 'INSERT' then
    if new.granted_by is distinct from auth.uid() or new.revoked_at is not null or new.revoked_by is not null then
      raise exception 'STANDING_OWNER_ONLY' using errcode = '42501';
    end if;
    if not exists (select 1 from app.kb_facts f where f.customer_id = new.customer_id and f.topic = new.topic
                     and f.approved_by_owner and app.fact_hash(f.fact) = new.fact_hash) then
      raise exception 'STANDING_FACT_NOT_APPROVED' using errcode = 'P0001';
    end if;
    new.granted_at := now();
    return new;
  end if;
  if (new.customer_id, new.topic, new.fact_hash, new.granted_by, new.granted_at)
     is distinct from (old.customer_id, old.topic, old.fact_hash, old.granted_by, old.granted_at)
     or old.revoked_at is not null or new.revoked_at is null or new.revoked_by is distinct from auth.uid() then
    raise exception 'STANDING_IMMUTABLE' using errcode = 'P0001';
  end if;
  new.revoked_at := now();
  return new;
end $$;
create trigger standing_approvals_guard before insert or update on app.standing_approvals
  for each row execute function app.standing_approvals_guard();

-- approvals: how a decision was made; set by the trigger only
alter table app.approvals add column decided_via text check (decided_via in ('owner', 'standing'));
create or replace function app.approvals_before_write() returns trigger
language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
begin
  if tg_op = 'INSERT' then
    if new.decision <> 'pending' or new.decided_by is not null or new.decided_at is not null
       or new.consumed_at is not null or new.consumed_by_ref is not null or new.decided_via is not null then
      raise exception 'PROPOSAL_MUST_BE_PENDING' using errcode = 'P0001';
    end if;
    new.payload_hash := app.canonical_hash(new.payload);
    return new;
  end if;
  if (new.payload, new.target_id, new.proposal_action, new.customer_id, new.scope, new.requested_by_agent, new.requested_at, new.expires_at)
     is distinct from (old.payload, old.target_id, old.proposal_action, old.customer_id, old.scope, old.requested_by_agent, old.requested_at, old.expires_at) then
    raise exception 'APPROVAL_IMMUTABLE' using errcode = 'P0001';
  end if;
  new.payload_hash := old.payload_hash;
  if old.decision <> 'pending' and (new.decision, new.decided_via) is distinct from (old.decision, old.decided_via) then
    raise exception 'DECISION_FINAL' using errcode = 'P0001';
  end if;
  if old.decision = 'pending' and new.decision in ('approved','rejected') then
    if old.expires_at <= now() then raise exception 'APPROVAL_EXPIRED' using errcode = 'P0001'; end if;
    if current_user = 'hermes_standing' then          -- only app.approve_by_standing runs as this role
      if coalesce(current_setting('app.standing_approval', true), '') <> old.id::text or new.decision <> 'approved' then
        raise exception 'STANDING_SCOPE' using errcode = '42501';
      end if;
      new.decided_via := 'standing';
    else
      if new.decided_by is distinct from auth.uid() or auth.uid() is null then raise exception 'DECIDER_MUST_BE_SESSION_USER' using errcode = '42501'; end if;
      if old.scope = 'customer' and not exists (select 1 from app.customer_users u
                                                 where u.customer_id = old.customer_id and u.auth_user_id = auth.uid() and u.active) then
        raise exception 'CUSTOMER_APPROVAL_OWNER_ONLY' using errcode = '42501';
      end if;
      if old.scope = 'platform' and not app.is_operator() then
        raise exception 'PLATFORM_APPROVAL_OPERATOR_AAL2_ONLY' using errcode = '42501';
      end if;
      new.decided_via := 'owner';
    end if;
    new.decided_at := now();
  elsif new.decided_via is distinct from old.decided_via then
    raise exception 'DECISION_FINAL' using errcode = 'P0001';
  end if;
  if (new.consumed_at, new.consumed_by_ref) is distinct from (old.consumed_at, old.consumed_by_ref) then
    if old.consumed_at is not null then
      raise exception 'APPROVAL_CONSUMED_FINAL' using errcode = 'P0001';
    end if;
    if current_user <> (select pg_catalog.pg_get_userbyid(c.relowner) from pg_catalog.pg_class c where c.oid = 'app.approvals'::regclass) then
      raise exception 'CONSUME_VIA_OUTBOX_ONLY' using errcode = '42501';
    end if;
  end if;
  return new;
end $$;

-- what hermes_standing may read and decide: approved facts (no other column of the business), pending reply proposals
grant select (customer_id, topic, fact, approved_by_owner, valid_until) on app.kb_facts to hermes_standing;
create policy kb_facts_standing_read on app.kb_facts for select to hermes_standing using (approved_by_owner);
grant select (id, customer_id, proposal_action, payload, decision, expires_at) on app.approvals to hermes_standing;
grant update (decision, decided_by) on app.approvals to hermes_standing;
create policy approvals_standing_read on app.approvals for select to hermes_standing       -- the updated row is read back
  using (proposal_action = 'reply:send' and (decision = 'pending' or decided_via = 'standing'));
create policy approvals_standing_decide on app.approvals for update to hermes_standing
  using (decision = 'pending' and proposal_action = 'reply:send') with check (decision = 'approved');
grant execute on function app.worker_customer_id () to hermes_standing;   -- (spaced: tools/validate.py reads the definition by name)

create function app.approve_by_standing(p_approval uuid) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare c uuid; a record; f text; n int; g uuid;
begin
  c := app.worker_customer_id();                       -- the lease decides the tenant, never an argument
  if c is null then return false; end if;
  select id, payload, expires_at into a from app.approvals
   where id = p_approval and customer_id = c and proposal_action = 'reply:send' and decision = 'pending' for update;
  if not found or a.expires_at <= now() then return false; end if;
  select count(*), min(fact) into n, f from app.kb_facts
   where customer_id = c and topic = a.payload->>'topic' and approved_by_owner and (valid_until is null or valid_until >= current_date);
  if n <> 1 or f is distinct from a.payload->>'body' then return false; end if;
  select granted_by into g from app.standing_approvals
   where customer_id = c and topic = a.payload->>'topic' and revoked_at is null and fact_hash = app.fact_hash(f);
  if g is null then return false; end if;
  perform set_config('app.standing_approval', a.id::text, true);
  update app.approvals set decision = 'approved', decided_by = g where id = a.id;
  perform set_config('app.standing_approval', '', true);
  return true;
end $$;
revoke execute on function app.approve_by_standing(uuid) from public;
grant execute on function app.approve_by_standing(uuid) to hermes_worker;
grant hermes_standing to current_user;
grant create on schema app to hermes_standing;
alter function app.approve_by_standing(uuid) owner to hermes_standing;
revoke create on schema app from hermes_standing;
do $$ begin execute format('revoke hermes_standing from %I', current_user); end $$;

commit;
