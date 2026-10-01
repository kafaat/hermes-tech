-- 0024 · photo posts to a TikTok account, under the same owner approval as Facebook and Instagram (0023)
-- TikTok's Content Posting API publishes photos pulled from a URL (on a domain the app has verified) and runs
-- asynchronously: the request returns a publish id, the post appears later. An app TikTok has not yet audited may
-- publish only privately (SELF_ONLY), for at most five users a day; the privacy level is configuration
-- (HERMES_TIKTOK_PRIVACY, SELF_ONLY by default), never a field the owner or the payload chooses.
-- TikTok accounts are identified by an open_id (letters, digits, "_", "-", "."), not a number, hence the wider id.
alter type app.channel_kind add value if not exists 'tiktok_business';      -- outside the transaction: used below as text

begin;

alter table app.content_items drop constraint content_items_platform_check;
alter table app.content_items add constraint content_items_platform_check
  check (platform in ('facebook', 'instagram', 'tiktok', 'site', 'whatsapp'));
alter table app.content_items drop constraint content_items_account_id_check;
alter table app.content_items add constraint content_items_account_id_check
  check (account_id is null or account_id ~ '^[A-Za-z0-9_.-]{5,64}$');

drop policy content_items_owner_draft on app.content_items;
create policy content_items_owner_draft on app.content_items for insert to authenticated
  with check (customer_id in (select app.current_user_customer_ids()) and status = 'draft' and approval_id is null
              and published_at is null and kind = 'post' and platform in ('facebook', 'instagram', 'tiktok') and account_id is not null);

create or replace function app.content_target_guard() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  if new.platform in ('facebook', 'instagram', 'tiktok') and new.kind = 'post' and new.account_id is not null then
    if not exists (
         select 1 from app.channel_accounts a where a.customer_id = new.customer_id and a.external_id = new.account_id
            and a.status = 'active'
            and a.kind::text = case new.platform when 'facebook' then 'facebook_page' when 'instagram' then 'instagram_business'
                                                 else 'tiktok_business' end) then
      raise exception 'CONTENT_ACCOUNT_NOT_LINKED' using errcode = 'P0001';
    end if;
    if new.platform in ('instagram', 'tiktok') and new.image_url is null then
      raise exception 'CONTENT_IMAGE_REQUIRED' using errcode = 'P0001';     -- both publish an image, not text alone
    end if;
  end if;
  if tg_op = 'UPDATE' and (new.account_id, new.image_url) is distinct from (old.account_id, old.image_url) and old.status <> 'draft' then
    raise exception 'PUBLISHED_CONTENT_IMMUTABLE' using errcode = 'P0001';
  end if;
  return new;
end $$;

commit;
