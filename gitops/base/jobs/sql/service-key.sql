\getenv service_key_hash SERVICE_API_KEY_HASH
BEGIN;
UPDATE api_keys SET is_active = false
WHERE name = 'evaluation-service' AND key_hash <> :'service_key_hash';
INSERT INTO api_keys (name, key_hash, is_active)
VALUES ('evaluation-service', :'service_key_hash', true)
ON CONFLICT (key_hash) DO UPDATE SET is_active = true;
COMMIT;
