#!/usr/bin/env bash
# Host-side watch for the founder's Hermes Agent (runs on the host, not in the container).
# Prints metrics consumed by the alert rules in ops/slo.yaml. NOT executed in the build environment.
set -u
cd "$(dirname "$0")"
since="24 hours ago"
denied=$(awk -v t="$(date -d "$since" +%s)" '$1 >= t && /TCP_DENIED/' logs/access.log 2>/dev/null | wc -l)
errors=$(journalctl CONTAINER_NAME=hermes --since "$since" -p err --no-pager 2>/dev/null | wc -l)
health=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q hermes)" 2>/dev/null || echo unknown)
echo "hermes_agent_egress_denied_24h $denied"
echo "hermes_agent_errors_24h $errors"
echo "hermes_agent_health $health"
