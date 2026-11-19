#!/bin/sh
set -eu
: "${KUBECONFIG:?Defina KUBECONFIG para o cluster kind local}"
context=kind-toggle-observability
server=$(kubectl config view --minify --context "$context" -o 'jsonpath={.clusters[0].cluster.server}')
case "$server" in
  https://127.0.0.1:*) ;;
  *) echo "Contexto não aponta para a API local; interrompido." >&2; exit 1 ;;
esac
for target in toggle/telemetry-auth observability/telemetry-agent-auth observability/telemetry-gateway-auth; do
  namespace=${target%/*}
  name=${target#*/}
  if [ -n "$(kubectl --context "$context" -n "$namespace" get secret "$name" --ignore-not-found -o name)" ]; then
    echo "Secret $target já existe; não será substituído." >&2
    exit 1
  fi
done
umask 077
dir=$(mktemp -d)
cleanup() {
  rm -f -- "$dir/token" "$dir/hmac-key" "$dir/headers" "$dir/tls.key" "$dir/tls.crt"
  rmdir "$dir"
}
trap cleanup EXIT HUP INT TERM
openssl rand -hex 32 > "$dir/token"
openssl rand -hex 32 > "$dir/hmac-key"
python3 - "$dir" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
token = (root / "token").read_text().strip()
(root / "headers").write_text("Authorization=Bearer%20" + token)
PY
openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
  -keyout "$dir/tls.key" -out "$dir/tls.crt" \
  -subj '/CN=otel-collector.observability.svc.cluster.local' \
  -addext 'subjectAltName=DNS:otel-collector,DNS:otel-collector.observability.svc.cluster.local' >/dev/null 2>&1
kubectl --context "$context" -n toggle create secret generic telemetry-auth \
  --from-file=token="$dir/token" --from-file=hmac-key="$dir/hmac-key" \
  --from-file=headers="$dir/headers" --from-file=tls.crt="$dir/tls.crt" \
  --from-file=tls.key="$dir/tls.key"
kubectl --context "$context" -n observability create secret generic telemetry-agent-auth \
  --from-file=token="$dir/token" --from-file=tls.crt="$dir/tls.crt"
kubectl --context "$context" -n observability create secret generic telemetry-gateway-auth \
  --from-file=token="$dir/token" --from-file=tls.crt="$dir/tls.crt" --from-file=tls.key="$dir/tls.key"
