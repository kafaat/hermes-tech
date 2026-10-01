-- 0023 · posts to a Facebook page and an Instagram account: the owner writes, approves, and only then it is published
-- The approval and the published-content guard exist since 0009 (a post is 'published' only through its own
-- approval: same business, same content id, same payload hash, consumed by the outbox). What was missing:
--   a draft the owner can write    content_items_owner_draft: own business, status draft, kind post, Facebook or
--                                  Instagram, an active linked account of that kind; Instagram needs an image URL
--   the proposal                   the owner queues content.propose (tasks_owner_content_propose, for a draft of
--                                  their own); the worker checks the text (content_guard) and proposes content:publish
--   what is approved               content_payload now carries the account and the image URL, so the owner approves
--                                  exactly where it goes and what it shows; the outbox sends exactly that
begin;

alter table app.content_items
  add column account_id text check (account_id is null or account_id ~ '^[0-9]{5,25}$'),
  add column image_url  text check (image_url is null or (image_url ~ '^https://[^[:space:]"<>\\]+$'
                                                        and length(image_url) between 12 and 2000));   -- PG caps {m,n} at 255

create or replace function app.content_payload(c app.content_items) returns jsonb
  language sql immutable as $$
  select jsonb_build_object('content_id', c.id, 'body', c.body, 'media_ids', to_jsonb(c.media_ids), 'platform', c.platform,
                            'account_id', c.account_id, 'image_url', c.image_url)
$$;

create policy content_items_owner_draft on app.content_items for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()) and status = 'draft' and approval_id is null
              and published_at is null and kind = 'post' and platform in ('facebook', 'instagram') and account_id is not null);

create function app.content_target_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if new.platform in ('facebook', 'instagram') and new.kind = 'post' and new.account_id is not null then
    if not exists (
         select 1 from app.channel_accounts a where a.customer_id = new.customer_id and a.external_id = new.account_id
            and a.status = 'active' and a.kind = (case new.platform when 'facebook' then 'facebook_page' else 'instagram_business' end)::app.channel_kind) then
      raise exception 'CONTENT_ACCOUNT_NOT_LINKED' using errcode = 'P0001';
    end if;
    if new.platform = 'instagram' and new.image_url is null then
      raise exception 'CONTENT_IMAGE_REQUIRED' using errcode = 'P0001';     -- Instagram publishes an image, not text alone
    end if;
  end if;
  if tg_op = 'UPDATE' and (new.account_id, new.image_url) is distinct from (old.account_id, old.image_url) and old.status <> 'draft' then
    raise exception 'PUBLISHED_CONTENT_IMMUTABLE' using errcode = 'P0001';
  end if;
  return new;
end $$;
create trigger content_target_guard before insert or update on app.content_items
  for each row execute function app.content_target_guard();

revoke insert on app.tasks from authenticated;                -- operators keep what resolve_outbox inserts (0015)
grant insert (public_ref, customer_id, agent_id, kind, idempotency_key) on app.tasks to authenticated;
create policy tasks_owner_content_propose on app.tasks for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()) and agent_id = 'agent_triage'
              and kind = 'content.propose' and status = 'queued' and attempts = 0
              and exists (select 1 from app.content_items c where 'content:' || c.id::text = idempotency_key
                             and c.customer_id = tasks.customer_id and c.status = 'draft'));

commit;
