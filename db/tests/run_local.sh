#!/usr/bin/env bash
# First gate before the pilot: run every isolation and catalog case and the three race scripts on a real,
# disposable Postgres 15. One command on any machine with Docker. NOT executed in the build environment.
set -euo pipefail
cd "$(dirname "$0")/../.."
name=hermes-rls-$$
docker run -d --rm --name "$name" -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:15 >/dev/null
trap 'docker stop "$name" >/dev/null' EXIT
export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres
for i in $(seq 1 30); do pg_isready -q && break; sleep 1; done
bash db/tests/run_isolation.sh
bash db/tests/concurrency_reserve.sh
bash db/tests/concurrency_outbox.sh
bash db/tests/concurrency_lease.sh
echo "local gate: PASS"
