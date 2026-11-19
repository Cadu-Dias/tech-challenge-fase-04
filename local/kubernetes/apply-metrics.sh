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
work_dir=$(mktemp -d)
trap 'rm -f -- "$work_dir/prometheus.yml" "$work_dir/kubelet.crt"; rmdir "$work_dir"' EXIT
node=toggle-observability-control-plane
cluster=$(docker inspect --format '{{ index .Config.Labels "io.x-k8s.kind.cluster" }}' "$node")
[ "$cluster" = toggle-observability ] || { echo "Nó Docker não pertence ao kind esperado." >&2; exit 1; }
docker exec "$node" cat /var/lib/kubelet/pki/kubelet.crt > "$work_dir/kubelet.crt"
openssl x509 -in "$work_dir/kubelet.crt" -noout -checkhost "$node" >/dev/null
openssl x509 -in "$work_dir/kubelet.crt" -noout -checkend 0 >/dev/null
kubectl --context "$context" get node "$node" -o json | ruby local/kubernetes/generate-prometheus.rb > "$work_dir/prometheus.yml"
kubectl --context "$context" -n toggle create configmap kubelet-serving-cert \
  --from-file=kubelet.crt="$work_dir/kubelet.crt" --dry-run=client -o yaml |
  kubectl --context "$context" apply -f -
kubectl --context "$context" -n toggle create configmap prometheus-config \
  --from-file=prometheus.yml="$work_dir/prometheus.yml" --dry-run=client -o yaml |
  kubectl --context "$context" apply -f -
kubectl --context "$context" apply -f local/kubernetes/kubernetes-metrics.yaml
kubectl --context "$context" -n toggle patch deployment prometheus \
  --type strategic --patch-file local/kubernetes/prometheus-metrics-patch.yaml
kubectl --context "$context" -n toggle rollout restart deployment/prometheus
kubectl --context "$context" -n toggle rollout status deployment/kube-state-metrics-local --timeout=150s
kubectl --context "$context" -n toggle rollout status deployment/prometheus --timeout=150s
