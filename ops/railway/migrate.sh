#!/usr/bin/env bash
# Railway staging job: apply every migration not yet applied, as the plain owner hermes_owner (as in CI),
# then run the isolation cases (one transaction, rolled back) against the real database, and, when
# HERMES_APP_URL is set, the end-to-end run of the pilot path against that running service (db/tests/e2e_pilot.py).
# MIGRATE_ONLY=1 applies the migrations and stops: hermes-app runs it as its pre-deploy command, so a new build
# never starts against an unmigrated schema, and a failed migration leaves the previous deployment serving.
# Staging only: db/local/0000_supabase_shim.sql emulates Supabase auth and is NOT for production.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DATABASE_URL:?DATABASE_URL is required (Railway: \${{Postgres.DATABASE_URL}})}"
db() { psql "$DATABASE_URL" -q -v ON_ERROR_STOP=1 "$@"; }

db -f db/local/0000_supabase_shim.sql
db -c "create table if not exists public.schema_migrations (name text primary key, applied_at timestamptz not null default now())"
for f in db/migrations/*.sql; do
  name=$(basename "$f")
  if [ "$(db -tAc "select count(*) from public.schema_migrations where name = '$name'")" = 1 ]; then
    echo "== $name already applied"; continue
  fi
  echo "== $name (as hermes_owner)"
  PGOPTIONS="-c role=hermes_owner" db -f "$f"
  db -c "insert into public.schema_migrations (name) values ('$name')"
done

if [ "${MIGRATE_ONLY:-}" = 1 ]; then echo "migrations: up to date"; exit 0; fi   # hermes-app pre-deploy

expected=$(grep -o "raise notice 'PASS" db/tests/rls_isolation_test.sql | wc -l)
out=$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/tests/rls_isolation_test.sql 2>&1) || { echo "$out"; echo "isolation: FAILED"; exit 1; }
got=$(grep -c "NOTICE:  PASS" <<<"$out" || true)
[ "$got" -eq "$expected" ] || { echo "$out"; echo "isolation: $got/$expected PASS notices"; exit 1; }
echo "isolation: $got/$expected PASS"

if [ -n "${HERMES_APP_URL:-}" ]; then
  echo "== e2e against $HERMES_APP_URL"
  python3 db/tests/e2e_pilot.py
fi
