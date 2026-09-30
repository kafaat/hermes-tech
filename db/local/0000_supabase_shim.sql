-- Local testing on plain Postgres 15+: emulate the parts of Supabase the migrations use.
-- NOT for production. Run as superuser before 0001.
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
create or replace function auth.jwt() returns jsonb language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claims', true), '')::jsonb, '{}'::jsonb)
$$;
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role')  then create role service_role nologin bypassrls; end if;
  -- v1.8: the migrations run as a PLAIN owner (no superuser, no bypassrls), as they would under an
  -- owner that row security applies to. Under a superuser owner FORCE is moot and a definer function
  -- that silently reads nothing from a forced table would still pass (review of 1.7, §4).
  if not exists (select 1 from pg_roles where rolname = 'hermes_owner') then
    create role hermes_owner nologin nosuperuser nobypassrls createrole;
  end if;
end $$;
create extension if not exists pgcrypto;          -- a trusted extension, created here so the owner needs no CREATE on public
grant usage on schema auth to authenticated, hermes_owner;
do $$ begin execute format('grant create on database %I to hermes_owner', current_database()); end $$;
