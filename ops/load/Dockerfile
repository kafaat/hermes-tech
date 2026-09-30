# Railway service load-test: k6 against the staging webhook, then verify_load.py against the staging database.
# Watch pattern ops/load/** (runs when the load test changes, or on a manual redeploy); never restarts.
FROM grafana/k6:1.8.1 AS k6
FROM python:3.12-slim-bookworm
COPY --from=k6 /usr/bin/k6 /usr/bin/k6
WORKDIR /app
COPY requirements-service.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-service.txt
COPY . .
CMD ["bash", "ops/load/run.sh"]
