# Railway staging image for both services (dockerfile path, start command, restart policy and healthcheck are
# set on each service in Railway; config-as-code files are deprecated there):
#   hermes-app  python -m service.app           webhook endpoint + worker loop (P1); healthcheck /healthz; restart on failure
#   db-migrate  bash ops/railway/migrate.sh     migrations, isolation cases, then the e2e run against hermes-app; never restarts
# Debian bookworm: its postgresql-client is 15, matching the CI database.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-service.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements-service.txt
COPY . .
CMD ["python", "-m", "service.app"]
