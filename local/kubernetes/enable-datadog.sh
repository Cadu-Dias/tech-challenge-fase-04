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
rollback() {
  if [ "${activation_started:-no}" = yes ]; then
    echo "Verificação ou ativação falhou; removendo o pipeline Datadog." >&2
    if ! sh local/kubernetes/disable-datadog.sh; then
      echo "Não foi possível desligar o pipeline; intervenção local necessária." >&2
    fi
  fi
}
activation_started=yes
trap rollback EXIT
python3 local/kubernetes/test-sanitize.py
python3 local/kubernetes/smoke-otlp-auth.py
python3 local/kubernetes/smoke-forged-context.py
python3 local/kubernetes/smoke.py
kubectl --context "$context" -n observability create configmap collector-datadog \
  --from-file=local/kubernetes/datadog-pipeline.yaml --dry-run=client -o yaml |
  kubectl --context "$context" apply -f -
kubectl --context "$context" -n observability patch deployment otel-collector \
  --type strategic --patch-file local/kubernetes/datadog-patch.yaml
kubectl --context "$context" -n observability rollout status deployment/otel-collector --timeout=150s
python3 local/kubernetes/smoke-otlp-auth.py
python3 local/kubernetes/smoke-forged-context.py
python3 local/kubernetes/test-sanitize.py
python3 local/kubernetes/smoke-logs.py
python3 local/kubernetes/smoke-datadog.py
activation_started=no
