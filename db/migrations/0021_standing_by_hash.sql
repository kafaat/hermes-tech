-- 0021 · a standing approval matches the granted text by its hash, however many approved facts the topic has
-- Found on staging (spec 28.20): the e2e customer had more than one approved "hours" fact, and approve_by_standing
-- required exactly one, so the granted answer waited for the owner. The grant already names the text (fact_hash):
-- the reply is decided when an approved, valid fact of that topic has exactly the reply's text and that hash.
-- The worker now picks the most recently updated approved fact of a topic (service/worker.py), not an arbitrary one.
begin;

grant hermes_standing to current_user;                 -- to replace a function owned by hermes_standing; ends below
create or replace function app.approve_by_standing(p_approval uuid) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare c uuid; a record; g uuid;
begin
  c := app.worker_customer_id();                       -- the lease decides the tenant, never an argument
  if c is null then return false; end if;
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
