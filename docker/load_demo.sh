#!/bin/sh
# Loads the university demo (Ontop tutorial) into its own database on first start.
# The tutorial data has no license file, so it is downloaded from the official repository instead of being bundled.
set -e
[ "${LOAD_DEMO:-1}" = "1" ] || exit 0
export PGHOST="${DEMO_DB_HOST:-postgres}" PGPORT="${DEMO_DB_PORT:-5432}" PGUSER="${DEMO_DB_USER:-tabulary}" PGPASSWORD="${DEMO_DB_PASSWORD:-tabulary}"
for i in $(seq 1 30); do psql -d "${DEMO_DB_NAME:-tabulary}" -tAc "select 1" >/dev/null 2>&1 && break; sleep 1; done
if psql -d "${DEMO_DB_NAME:-tabulary}" -tAc "select 1 from pg_database where datname='uni'" | grep -q 1; then
  echo "demo: university database already present"; exit 0
fi
echo "demo: loading the university demo from ontop/ontop-tutorial"
tmp=$(mktemp)
if curl -fsSL -o "$tmp" https://raw.githubusercontent.com/ontop/ontop-tutorial/master/basic/dataset-no-ssn.sql; then
  psql -d "${DEMO_DB_NAME:-tabulary}" -qc "create database uni"
  psql -d uni -q -f "$tmp"
else
  echo "demo: download failed (offline?); the university demo is skipped"
fi
rm -f "$tmp"
