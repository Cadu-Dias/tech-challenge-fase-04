#!/bin/bash
# Cria um banco por serviço e aplica o schema de cada um (mesma separação usada no RDS).
set -euo pipefail
for svc in auth flag targeting; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -c "CREATE DATABASE ${svc}_db;"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${svc}_db" -f "/schemas/${svc}.sql"
done
# Chave de serviço sintética usada pelo evaluation-service (apenas o hash SHA-256 é gravado).
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d auth_db <<'SQL'
\getenv service_key LOCAL_SERVICE_API_KEY
INSERT INTO api_keys (name, key_hash)
VALUES ('evaluation-service-local', encode(sha256(convert_to(:'service_key', 'UTF8')), 'hex'));
SQL
