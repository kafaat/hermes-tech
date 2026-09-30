# Railway staging migration job (see migrate.sh). Postgres 15 client, matching the CI database.
FROM postgres:15
WORKDIR /app
COPY db/ db/
COPY ops/railway/migrate.sh ops/railway/migrate.sh
CMD ["bash", "ops/railway/migrate.sh"]
