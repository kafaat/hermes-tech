#!/usr/bin/env bash
# The `database` job of CI (.github/workflows/validate.yml), on this machine, before a push: migrations as a PLAIN
# owner + every isolation and catalog case, the three race scripts, the build fence, the end-to-end pilot path, the
# hermes command line, and staging's auto-activation. Same scripts, same order, one database, as in CI.
#   bash db/tests/run_local.sh                   a disposable Postgres 15 in Docker (the CI image), removed afterwards
#   PGHOST=localhost PGPORT=5433 bash db/tests/run_local.sh
#                                                a server you already run: a scratch database is created, then dropped;
#                                                PGUSER (default postgres) must be a superuser, as in CI
# The Python scripts need the service lock: pip install -r requirements-service.txt
set -euo pipefail
cd "$(dirname "$0")/../.."
if [ -z "${PGHOST:-}" ]; then
  name=hermes-rls-$$
  docker run -d --rm --name "$name" -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:15 >/dev/null
  trap 'docker stop "$name" >/dev/null' EXIT
  export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres PGDATABASE=postgres
  for _ in $(seq 1 30); do pg_isready -q && break; sleep 1; done
else
  export PGUSER="${PGUSER:-postgres}" PGPORT="${PGPORT:-5432}"
  scratch="hermes_gate_$$"
  psql -d postgres -qtAc "create database $scratch"
  trap 'psql -d postgres -qtAc "drop database if exists $scratch with (force)" >/dev/null' EXIT
  export PGDATABASE="$scratch"
fi
export DATABASE_URL="postgresql://$PGUSER${PGPASSWORD:+:$PGPASSWORD}@$PGHOST:$PGPORT/$PGDATABASE"
bash db/tests/run_isolation.sh
bash db/tests/concurrency_reserve.sh
bash db/tests/concurrency_outbox.sh
bash db/tests/concurrency_lease.sh
python3 db/tests/concurrency_build_fence.py
python3 db/tests/e2e_pilot.py
python3 db/tests/ops_cli_pg.py
python3 db/tests/staging_auto_activation.py
echo "local gate: PASS"
