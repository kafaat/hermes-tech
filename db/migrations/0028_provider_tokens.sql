-- 0028 · linking a TikTok account by the owner's own consent, its tokens sealed and renewed (spec 28.30)
-- Until now a TikTok token was configuration (HERMES_TIKTOK_TOKENS), but a TikTok access token lives 24 hours and its
-- refresh token rotates: a configured token stops publishing a day later. Now:
--   linking       the owner signs in to TikTok and consents (OAuth, the portal's /portal/connect/tiktok); the portal
--                 calls app.link_provider_account, which links the account to the owner's own business only
--   at rest       app.provider_tokens holds AES-GCM ciphertext; the key is a platform secret on the host, never in the
--                 database (service/token_box.py): a copy of the database reveals no token
--   use           the worker of that business reads and renews them (lease-bound), under a row lock, before expiry
--   the owner     sees whether the link is healthy (expiry, failures), never a token
-- app.link_provider_account is owned by hermes_linker (NOLOGIN, like hermes_standing in 0020): narrow column grants and
-- policies, the caller's identity from the session (request.jwt.claim.sub / claims), never from an argument.
begin;

create table app.provider_tokens (
  customer_id        uuid not null references app.customers(id),
  provider           text not null check (provider in ('tiktok')),
  account_id         text not null check (account_id ~ '^[A-Za-z0-9_.-]{5,64}$'),
  access_ct          bytea not null check (length(access_ct) between 37 and 4096),
  refresh_ct         bytea not null check (length(refresh_ct) between 37 and 4096),
  access_expires_at  timestamptz not null,
  refresh_expires_at timestamptz not null,
  linked_by          uuid not null,
  rotated_at         timestamptz not null default now(),
  failures           int not null default 0 check (failures >= 0),
  last_error         text check (length(last_error) <= 120),
  primary key (provider, account_id)
);
create index on app.provider_tokens (customer_id);
alter table app.provider_tokens enable row level security;
alter table app.provider_tokens force row level security;

-- the worker of the leased business: read and renew, never insert or relink
create policy provider_tokens_worker_read on app.provider_tokens for select to hermes_worker
  using (customer_id = (select app.worker_customer_id()));
create policy provider_tokens_worker_renew on app.provider_tokens for update to hermes_worker
  using (customer_id = (select app.worker_customer_id())) with check (customer_id = (select app.worker_customer_id()));
grant select (customer_id, provider, account_id, access_ct, refresh_ct, access_expires_at, refresh_expires_at, failures)
  on app.provider_tokens to hermes_worker;
grant update (access_ct, refresh_ct, access_expires_at, refresh_expires_at, rotated_at, failures, last_error)
  on app.provider_tokens to hermes_worker;

-- the owner: the health of the link, never a token (column grants: no *_ct)
create policy provider_tokens_owner_read on app.provider_tokens for select to authenticated
  using (customer_id in (select app.current_user_customer_ids()));
grant select (customer_id, provider, account_id, refresh_expires_at, rotated_at, failures, last_error)
  on app.provider_tokens to authenticated;

-- linking: one function, owned by a role that can do nothing else
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'hermes_linker') then create role hermes_linker nologin noinherit; end if;
end $$;
grant usage on schema app to hermes_linker;
grant select (customer_id, auth_user_id, role, active) on app.customer_users to hermes_linker;
create policy customer_users_linker_read on app.customer_users for select to hermes_linker using (active);
grant select (customer_id, kind, external_id, status) on app.channel_accounts to hermes_linker;
grant insert (customer_id, kind, external_id, display_name, status, verified_at) on app.channel_accounts to hermes_linker;
grant update (display_name, status, verified_at) on app.channel_accounts to hermes_linker;
create policy channel_accounts_linker on app.channel_accounts for all to hermes_linker
  using (kind = 'tiktok_business') with check (kind = 'tiktok_business');
grant select (customer_id, provider, account_id) on app.provider_tokens to hermes_linker;
grant insert (customer_id, provider, account_id, access_ct, refresh_ct, access_expires_at, refresh_expires_at, linked_by)
  on app.provider_tokens to hermes_linker;
grant update (access_ct, refresh_ct, access_expires_at, refresh_expires_at, linked_by, rotated_at, failures, last_error)
  on app.provider_tokens to hermes_linker;
create policy provider_tokens_linker on app.provider_tokens for all to hermes_linker using (true) with check (true);

create function app.link_provider_account(p_customer uuid, p_provider text, p_account text, p_name text,
                                          p_access bytea, p_refresh bytea, p_access_expires timestamptz,
                                          p_refresh_expires timestamptz) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare u uuid; holder uuid;
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
  select customer_id into holder from app.channel_accounts where kind = 'tiktok_business' and external_id = p_account;
  if holder is not null and holder <> p_customer then
    raise exception 'LINK_ACCOUNT_TAKEN' using errcode = 'P0001';  -- one business per TikTok account
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
revoke execute on function app.link_provider_account(uuid, text, text, text, bytea, bytea, timestamptz, timestamptz) from public;
grant execute on function app.link_provider_account(uuid, text, text, text, bytea, bytea, timestamptz, timestamptz) to authenticated;
grant hermes_linker to current_user;
grant create on schema app to hermes_linker;
alter function app.link_provider_account(uuid, text, text, text, bytea, bytea, timestamptz, timestamptz) owner to hermes_linker;
revoke create on schema app from hermes_linker;
do $$ begin execute format('revoke hermes_linker from %I', current_user); end $$;

commit;
