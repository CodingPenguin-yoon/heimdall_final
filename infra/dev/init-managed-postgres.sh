#!/bin/sh
set -eu

: "${HEIMDALL_MANAGED_DB_PROVISIONER_PASSWORD:?required}"

psql --set=ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname postgres \
  --set=provisioner_password="${HEIMDALL_MANAGED_DB_PROVISIONER_PASSWORD}" <<'SQL'
SELECT format(
  'CREATE ROLE heimdall_provisioner LOGIN CREATEDB CREATEROLE NOINHERIT NOSUPERUSER PASSWORD %L',
  :'provisioner_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'heimdall_provisioner') \gexec

ALTER ROLE heimdall_provisioner
  WITH LOGIN CREATEDB CREATEROLE NOINHERIT NOSUPERUSER
  PASSWORD :'provisioner_password';

REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE postgres TO heimdall_provisioner;
SQL
