#!/bin/sh
set -eu
: "${KUBECONFIG:?Defina KUBECONFIG para o cluster kind local}"
context=kind-toggle-observability
server=$(kubectl config view --minify --context "$context" -o 'jsonpath={.clusters[0].cluster.server}')
case "$server" in
  https://127.0.0.1:*) ;;
  *) echo "Contexto não aponta para a API local; interrompido." >&2; exit 1 ;;
esac
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"
kubectl --context "$context" -n observability patch deployment otel-collector \
  --type strategic --patch-file local/kubernetes/datadog-disable-patch.yaml
kubectl --context "$context" -n observability rollout status deployment/otel-collector --timeout=150s
