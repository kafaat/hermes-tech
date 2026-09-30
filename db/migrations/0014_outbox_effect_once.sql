-- 0014 · an effect without an approval is enqueued once per (customer, topic, target)
-- Found by a staging rehearsal (spec 28.7): a worker stopped mid-send left its notify.owner row in 'sending'; the
-- task's lease expired, the next worker re-ran the task, and enqueue_outbox inserted a SECOND notice for the same
-- event and sent it. The owner was told twice, and the first row still waited for a human. 0010 made redelivery
-- idempotent only through the approval (approval_id); notify.owner has none. Now the target is the identity: the
-- re-run gets the existing row back, claim_outbox_dispatch finds its expired lease and flags it for a human
-- (never re-sent), and a different payload for the same target is refused.
begin;

create or replace function app.enqueue_outbox(p_topic text, p_payload jsonb, p_approval_id uuid, p_target_id text)
returns bigint language plpgsql security invoker set search_path = app, extensions, public, pg_temp as $$
declare o app.outbox; v_id bigint; v_customer uuid;
begin
  if p_approval_id is not null then
    select * into o from app.outbox where approval_id = p_approval_id;
    if found then
      if o.topic = p_topic and o.target_id is not distinct from p_target_id and o.payload_hash = app.canonical_hash(p_payload) then
        return o.id;
      end if;
      raise exception 'APPROVAL_ALREADY_CONSUMED' using errcode = 'P0001';
    end if;
    select customer_id into v_customer from app.approvals where id = p_approval_id;
  else
    v_customer := app.worker_customer_id();
    if p_target_id is null then raise exception 'OUTBOX_TARGET_REQUIRED' using errcode = '22023'; end if;
    select * into o from app.outbox
     where approval_id is null and customer_id is not distinct from v_customer and topic = p_topic and target_id = p_target_id
     order by id limit 1;
    if found then
      if o.payload_hash = app.canonical_hash(p_payload) then return o.id; end if;
      raise exception 'OUTBOX_TARGET_CONFLICT' using errcode = 'P0001';
    end if;
  end if;
  begin
    insert into app.outbox (customer_id, topic, payload, approval_id, target_id)
    values (v_customer, p_topic, p_payload, p_approval_id, p_target_id) returning id into v_id;
    return v_id;
  exception when unique_violation or raise_exception then     -- a concurrent delivery enqueued it first
    if sqlstate <> '23505' and (p_approval_id is null or sqlerrm <> 'APPROVAL_ALREADY_CONSUMED') then raise; end if;
    if p_approval_id is not null then
      select * into o from app.outbox where approval_id = p_approval_id;
    else
      select * into o from app.outbox where approval_id is null and customer_id is not distinct from v_customer
                                          and topic = p_topic and target_id = p_target_id order by id limit 1;
    end if;
    if found and o.topic = p_topic and o.target_id is not distinct from p_target_id and o.payload_hash = app.canonical_hash(p_payload) then
      return o.id;
    end if;
    raise;
  end;
end $$;

-- race-safe: two deliveries of one event cannot both insert. Rows from before this migration keep their history
-- (a duplicate already sent is a record, not something to delete), so the index starts after the last id issued.
-- Read from the id sequence, not max(id): the table is FORCE'd and the migration owner has no policy on it, so
-- max(id) is 0 to this role (the first run of this migration failed exactly so, on the rehearsal's duplicates).
do $$ declare seq text := pg_get_serial_sequence('app.outbox', 'id'); last bigint; called boolean; begin
  execute format('select last_value, is_called from %s', seq) into last, called;
  execute format('create unique index outbox_effect_once on app.outbox (customer_id, topic, target_id)'
                 ' where approval_id is null and id > %s', case when called then last else 0 end);
end $$;

commit;
