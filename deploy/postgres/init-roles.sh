#!/bin/bash
# Runs once, when the Postgres data directory is first created.
# Creates the migration owner and the application role. The application
# role is not a superuser and is not the table owner.
set -euo pipefail

sql_escape() {
  printf "%s" "$1" | sed "s/'/''/g"
}

owner="$(sql_escape "${CAPLOG_OWNER_PASSWORD}")"
app="$(sql_escape "${CAPLOG_APP_PASSWORD}")"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'caplog_owner') THEN
    CREATE ROLE caplog_owner LOGIN PASSWORD '${owner}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'caplog_app') THEN
    CREATE ROLE caplog_app LOGIN PASSWORD '${app}' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
\$\$;
ALTER DATABASE ${POSTGRES_DB} OWNER TO caplog_owner;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO caplog_owner, caplog_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO caplog_owner;
GRANT USAGE ON SCHEMA public TO caplog_app;
SQL
