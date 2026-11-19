#!/bin/sh
set -eu
: "${KUBECONFIG:?Defina KUBECONFIG para o cluster kind local}"
context=kind-toggle-observability
server=$(kubectl config view --minify --context "$context" -o 'jsonpath={.clusters[0].cluster.server}')
case "$server" in
  https://127.0.0.1:*) ;;
  *) echo "Contexto não aponta para a API local; interrompido." >&2; exit 1 ;;
esac
for target in toggle/auth-metrics-client observability/auth-metrics-gateway; do
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
  rm -f -- "$dir/token" "$dir/headers"
  rmdir "$dir"
}
trap cleanup EXIT
openssl rand -hex 32 > "$dir/token"
python3 - "$dir" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
token = (root / "token").read_text().strip()
(root / "headers").write_text("Authorization=Bearer%20" + token)
PY
kubectl --context "$context" -n toggle create secret generic auth-metrics-client --from-file=headers="$dir/headers"
kubectl --context "$context" -n observability create secret generic auth-metrics-gateway --from-file=token="$dir/token"
