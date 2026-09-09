#!/bin/sh
# HAPI FHIR gets its own database, separate from the application schema:
# clinical resources and our session/audit/usage tables are different concerns
# with different owners (ARCHITECTURE.md section 8).
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE hapi OWNER $POSTGRES_USER;
EOSQL
