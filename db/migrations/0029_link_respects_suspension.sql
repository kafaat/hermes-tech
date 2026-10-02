-- 0029 · a relink respects the operator: no re-activating a suspended or revoked account, no linking for an inactive
-- business (spec 28.30, review of 2026-10-02)
-- Found by review: link_provider_account set an existing account of the caller's own business back to 'active', so an
-- owner could undo an operator's suspension (status decides routing and publishing) by linking again; and a cancelled
-- business could link. The function now refuses a suspended or revoked account (LINK_CHANNEL_SUSPENDED) and a business
-- that is not active (LINK_OWNER_ONLY). hermes_linker reads the status of active businesses only.
begin;

grant select (id, status) on app.customers to hermes_linker;
create policy customers_linker_read on app.customers for select to hermes_linker using (status = 'active');

grant hermes_linker to current_user;                   -- to replace a function owned by hermes_linker; ends below
create or replace function app.link_provider_account(p_customer uuid, p_provider text, p_account text, p_name text,
                                          p_access bytea, p_refresh bytea, p_access_expires timestamptz,
                                          p_refresh_expires timestamptz) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare u uuid; holder uuid; held_status app.channel_status;
begin
  u := nullif(coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                       nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'), '')::uuid;
  if u is null or not exists (select 1 from app.customer_users where customer_id = p_customer and auth_user_id = u
                                 and role = 'owner' and active) then
    raise exception 'LINK_OWNER_ONLY' using errcode = '42501';     -- an owner of THIS business, from the session
  end if;
  if p_provider <> 'tiktok' then
    raise exception 'LINK_PROVIDER' using errcode = 'P0001';
  end if;
  if not exists (select 1 from app.customers where id = p_customer and status = 'active') then
    raise exception 'LINK_OWNER_ONLY' using errcode = '42501';     -- a business that is not active links nothing (0029)
  end if;
  select customer_id, status into holder, held_status from app.channel_accounts
   where kind = 'tiktok_business' and external_id = p_account;
  if holder is not null and holder <> p_customer then
    raise exception 'LINK_ACCOUNT_TAKEN' using errcode = 'P0001';  -- one business per TikTok account
  end if;
  if holder is not null and held_status in ('suspended', 'revoked') then
    raise exception 'LINK_CHANNEL_SUSPENDED' using errcode = 'P0001';   -- the operator's decision stands (0029)
  end if;
  if holder is null then
    insert into app.channel_accounts (customer_id, kind, external_id, display_name, status, verified_at)
    values (p_customer, 'tiktok_business', p_account, left(p_name, 120), 'active', now());
  else
    update app.channel_accounts set display_name = left(p_name, 120), status = 'active', verified_at = now()
     where kind = 'tiktok_business' and external_id = p_account;
  end if;
  update app.provider_tokens                                       -- a relink: new tokens, a clean record
     set access_ct = p_access, refresh_ct = p_refresh, access_expires_at = p_access_expires,
         refresh_expires_at = p_refresh_expires, linked_by = u, rotated_at = now(), failures = 0, last_error = null
   where provider = p_provider and account_id = p_account and customer_id = p_customer;   -- never another business's row
  if not found then                                                -- (no upsert: it would need to read the sealed columns)
    insert into app.provider_tokens (customer_id, provider, account_id, access_ct, refresh_ct, access_expires_at,
                                     refresh_expires_at, linked_by)
    values (p_customer, p_provider, p_account, p_access, p_refresh, p_access_expires, p_refresh_expires, u);
  end if;
  return true;
end $$;
do $$ begin execute format('revoke hermes_linker from %I', current_user); end $$;

commit;
