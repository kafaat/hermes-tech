#!/usr/bin/env bash
# Apply the shim (as superuser) and ALL migrations AS A PLAIN OWNER ROLE, then run the isolation test
# and require that EVERY PASS notice in the file was actually emitted (a skipped block fails the job too).
# MIGRATION_ROLE=postgres reproduces the pre-1.8 superuser-owner run for comparison.
set -euo pipefail
cd "$(dirname "$0")/../.."
role="${MIGRATION_ROLE:-hermes_owner}"
psql -q -v ON_ERROR_STOP=1 -f db/local/0000_supabase_shim.sql
for f in db/migrations/*.sql; do
  echo "== $f (as $role)"; PGOPTIONS="-c role=$role" psql -q -v ON_ERROR_STOP=1 -f "$f"
done
expected=$(grep -o "raise notice 'PASS" db/tests/rls_isolation_test.sql | wc -l)
out=$(psql -v ON_ERROR_STOP=1 -f db/tests/rls_isolation_test.sql 2>&1) || { echo "$out"; echo "isolation: FAILED"; exit 1; }
echo "$out" | grep -E "NOTICE:  (PASS|FAIL|INFO)" || true
got=$(grep -c "NOTICE:  PASS" <<<"$out" || true)
if [ "$got" -ne "$expected" ]; then echo "isolation: $got/$expected PASS notices"; exit 1; fi
echo "isolation: $got/$expected PASS"
