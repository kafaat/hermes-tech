-- 0026 · the owner's escalations by email (spec 28.27): a notice to the address the owner signed in with, no message text
-- From 1 October 2026 Meta charges for utility templates even inside the customer service window, so paging the owner
-- on WhatsApp costs money per notice; until now a live notice only waited in the portal. An owner (any active member of
-- the business) may now ask for notices by email, to exactly the address Supabase Auth verified for that login (the
-- email claim of the session: the sign-in code went to it), never a typed address. The notice names the reason and
-- links to the portal; the customer's message stays in the portal. The worker reads the addresses of its leased
-- business only, and only of members who are still active.
begin;

create table app.owner_notify (
  customer_id  uuid not null references app.customers(id),
  auth_user_id uuid not null,
  email        text not null check (email ~ '^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$' and length(email) <= 254),
  enabled      boolean not null,                    -- off keeps the row (no deletes for application roles)
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  primary key (customer_id, auth_user_id)
);
alter table app.owner_notify enable row level security;
alter table app.owner_notify force row level security;
create policy owner_notify_self_select on app.owner_notify for select to authenticated
  using (auth_user_id = (select auth.uid()) and customer_id in (select app.current_user_customer_ids()));
create policy owner_notify_self_insert on app.owner_notify for insert to authenticated
  with check (auth_user_id = (select auth.uid()) and customer_id in (select app.current_user_customer_ids())
              and email = lower(coalesce((select auth.jwt()) ->> 'email', '')));
create policy owner_notify_self_update on app.owner_notify for update to authenticated
  using (auth_user_id = (select auth.uid()) and customer_id in (select app.current_user_customer_ids()))
  with check (auth_user_id = (select auth.uid()) and customer_id in (select app.current_user_customer_ids())
              and email = lower(coalesce((select auth.jwt()) ->> 'email', '')));
create policy owner_notify_worker_read on app.owner_notify for select to hermes_worker
  using (customer_id = (select app.worker_customer_id()) and enabled
         and exists (select 1 from app.customer_users u where u.customer_id = owner_notify.customer_id
                        and u.auth_user_id = owner_notify.auth_user_id and u.active));
grant select on app.owner_notify to authenticated;
grant insert (customer_id, auth_user_id, email, enabled) on app.owner_notify to authenticated;
grant update (email, enabled) on app.owner_notify to authenticated;

create function app.owner_notify_touch() returns trigger
language plpgsql security invoker set search_path = app, pg_temp as $$
begin
  new.updated_at := now();
  return new;
end $$;
create trigger owner_notify_touch before update on app.owner_notify for each row execute function app.owner_notify_touch();
grant select (customer_id, email, auth_user_id) on app.owner_notify to hermes_worker;

create policy customer_users_worker_read on app.customer_users for select to hermes_worker      -- membership only
  using (customer_id = (select app.worker_customer_id()));
grant select (customer_id, auth_user_id, active) on app.customer_users to hermes_worker;

create index on app.owner_notify (customer_id);

commit;
