#!/bin/sh
set -eu
: "${KUBECONFIG:?Defina KUBECONFIG para o cluster kind local}"
context=kind-toggle-observability
server=$(kubectl config view --minify --context "$context" -o 'jsonpath={.clusters[0].cluster.server}')
case "$server" in
  https://127.0.0.1:*) ;;
  *) echo "Contexto não aponta para a API local; interrompido." >&2; exit 1 ;;
esac
for resource in secret/toggle-self-healing-webhook secret/toggle-self-healing-tls \
  configmap/toggle-self-healing-state deployment/toggle-self-healing; do
  if [ -n "$(kubectl --context "$context" -n toggle get "$resource" --ignore-not-found -o name)" ]; then
    echo "$resource já existe; instalação não será substituída." >&2
    exit 1
  fi
done
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"
umask 077
dir=$(mktemp -d)
cleanup() {
  rm -f -- "$dir/token" "$dir/tls.crt" "$dir/tls.key"
  rmdir "$dir"
}
trap cleanup EXIT
openssl rand -hex 32 > "$dir/token"
openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
  -keyout "$dir/tls.key" -out "$dir/tls.crt" \
  -subj '/CN=toggle-self-healing.toggle.svc.cluster.local' \
  -addext 'subjectAltName=DNS:toggle-self-healing.toggle.svc.cluster.local,IP:127.0.0.1' >/dev/null 2>&1
kubectl --context "$context" -n toggle create secret generic toggle-self-healing-webhook \
  --from-file=token="$dir/token"
kubectl --context "$context" -n toggle create secret tls toggle-self-healing-tls \
  --cert="$dir/tls.crt" --key="$dir/tls.key"
kubectl --context "$context" apply -f automation/self-healing/kubernetes.yaml
kubectl --context "$context" -n toggle rollout status deployment/toggle-self-healing --timeout=150s
python3 local/kubernetes/setup-grafana-responder.py
