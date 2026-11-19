#!/bin/sh
set -eu

: "${KUBECONFIG:?Defina KUBECONFIG para o arquivo exclusivo do cluster kind local}"
context=kind-toggle-observability
server=$(kubectl config view --minify --context "$context" -o 'jsonpath={.clusters[0].cluster.server}')
case "$server" in
  https://127.0.0.1:*) ;;
  *) echo "Contexto não aponta para a API local; interrompido." >&2; exit 1 ;;
esac
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"
work_dir=$(mktemp -d)
trap 'rm -f -- "$work_dir/auth-5xx.yaml" "$work_dir/prometheus.yml" "$work_dir/kubelet.crt" "$work_dir/telegram-contactpoint.json" "$work_dir/auth-http-notifications.json" "$work_dir/self-healing-contactpoint.yaml"; rmdir "$work_dir"' EXIT

node=toggle-observability-control-plane
cluster=$(docker inspect --format '{{ index .Config.Labels "io.x-k8s.kind.cluster" }}' "$node")
[ "$cluster" = toggle-observability ] || { echo "Nó Docker não pertence ao kind esperado." >&2; exit 1; }
docker exec "$node" cat /var/lib/kubelet/pki/kubelet.crt > "$work_dir/kubelet.crt"
openssl x509 -in "$work_dir/kubelet.crt" -noout -checkhost "$node" >/dev/null
openssl x509 -in "$work_dir/kubelet.crt" -noout -checkend 0 >/dev/null
kubectl --context "$context" get node "$node" -o json | ruby local/kubernetes/generate-prometheus.rb > "$work_dir/prometheus.yml"

apply_config() {
  namespace=$1
  shift
  kubectl --context "$context" -n "$namespace" create configmap "$@" --dry-run=client -o yaml |
    kubectl --context "$context" apply -f -
}
apply_secret() {
  kubectl --context "$context" -n toggle create secret generic "$@" --dry-run=client -o yaml |
    kubectl --context "$context" apply -f -
}

apply_config toggle postgres-init --from-file=local/postgres/init-databases.sh
apply_config toggle postgres-schemas \
  --from-file=auth.sql=services/auth-service/db/init.sql \
  --from-file=flag.sql=services/flag-service/db/init.sql \
  --from-file=targeting.sql=services/targeting-service/db/init.sql
apply_config toggle localstack-init --from-file=local/localstack/init-aws.sh
apply_config observability collector-config --from-file=config.yaml=local/kubernetes/collector.yaml
apply_config observability agent-config --from-file=agent.yaml=local/kubernetes/agent.yaml
apply_config toggle prometheus-config --from-file=prometheus.yml="$work_dir/prometheus.yml"
apply_config toggle kubelet-serving-cert --from-file=kubelet.crt="$work_dir/kubelet.crt"
apply_config toggle tempo-config --from-file=local/tempo/tempo.yaml
apply_config observability grafana-datasources --from-file=datasources.yml=local/kubernetes/datasources.yaml
apply_config observability grafana-providers --from-file=local/grafana/provisioning/dashboards/dashboards.yml
apply_config observability grafana-dashboards --from-file=gitops/observability/grafana/dashboards/toggle-overview.json
ruby local/kubernetes/local-auth-alert.rb > "$work_dir/auth-5xx.yaml"
notification_keys=$(kubectl --context "$context" -n observability get secret notification-credentials \
  -o go-template='{{range $k,$v := .data}}{{if $v}}{{$k}}{{"\n"}}{{end}}{{end}}' 2>/dev/null | sort || true)
set -- --from-file="$work_dir/auth-5xx.yaml" \
  --from-file=gitops/observability/grafana/alerts/http-services.yaml
if [ "$notification_keys" = "$(printf 'pagerduty-key\nteams-webhook')" ]; then
  set -- "$@" --from-file=local/kubernetes/external-contactpoints.yaml
fi
self_healing_keys=$(kubectl --context "$context" -n observability get secret self-healing-client \
  -o go-template='{{range $k,$v := .data}}{{if $v}}{{$k}}{{"\n"}}{{end}}{{end}}' 2>/dev/null | sort || true)
discord_keys=$(kubectl --context "$context" -n observability get secret discord-webhook \
  -o go-template='{{range $k,$v := .data}}{{if $v}}{{$k}}{{"\n"}}{{end}}{{end}}' 2>/dev/null | sort || true)
telegram_keys=$(kubectl --context "$context" -n observability get secret telegram-alerts \
  -o go-template='{{range $k,$v := .data}}{{if $v}}{{$k}}{{"\n"}}{{end}}{{end}}' 2>/dev/null | sort || true)
kubectl --context "$context" -n observability get configmap grafana-alerts --ignore-not-found \
  -o 'jsonpath={.data.auth-http-notifications\.json}' > "$work_dir/auth-http-notifications.json"
if [ "$self_healing_keys" = "$(printf 'tls.crt\ntoken')" ]; then
  if [ "$discord_keys" = "url" ]; then
    self_healing_file=local/kubernetes/self-healing-discord.yaml
  else
    self_healing_file=local/kubernetes/self-healing-contactpoint.yaml
  fi
  if [ -s "$work_dir/auth-http-notifications.json" ]; then
    ruby -ryaml -e 'doc = YAML.safe_load(File.read(ARGV[0])); doc.delete("policies"); puts YAML.dump(doc)' \
      "$self_healing_file" > "$work_dir/self-healing-contactpoint.yaml"
    self_healing_file="$work_dir/self-healing-contactpoint.yaml"
  fi
  set -- "$@" --from-file=self-healing-contactpoint.yaml="$self_healing_file"
fi
if [ -s "$work_dir/auth-http-notifications.json" ]; then
  set -- "$@" --from-file=auth-http-notifications.json="$work_dir/auth-http-notifications.json"
fi
if [ "$telegram_keys" = "$(printf 'bot-token\nchat-id')" ]; then
  kubectl --context "$context" -n observability get secret telegram-alerts -o json |
    ruby -rjson -rbase64 -e '
      config = JSON.parse(File.read(ARGV[0]))
      secret = JSON.parse(STDIN.read)
      config["contactPoints"][0]["receivers"][0]["settings"]["chatid"] =
        Base64.decode64(secret.fetch("data").fetch("chat-id"))
      puts JSON.pretty_generate(config)
    ' local/kubernetes/telegram-contactpoint.json > "$work_dir/telegram-contactpoint.json"
  set -- "$@" --from-file=telegram-contactpoint.json="$work_dir/telegram-contactpoint.json"
fi
apply_config observability grafana-alerts "$@"
apply_config toggle local-common \
  --from-literal=OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.observability.svc.cluster.local:4318 \
  --from-literal=OTEL_RESOURCE_ATTRIBUTES=deployment.environment=local \
  --from-literal=AWS_REGION=us-east-1 \
  --from-literal=AWS_ACCESS_KEY_ID=test \
  --from-literal=AWS_SECRET_ACCESS_KEY=test \
  --from-literal=AWS_ENDPOINT_URL=http://localstack:4566 \
  --from-literal=AWS_SQS_ENDPOINT_URL=http://localstack:4566 \
  --from-literal=AWS_SQS_URL=http://localstack:4566/000000000000/togglemaster-evaluation-events \
  --from-literal=AWS_DYNAMODB_TABLE=ToggleMasterAnalytics

# These are disposable synthetic credentials, never real account credentials.
apply_secret auth-secret \
  --from-literal='DATABASE_URL=postgres://togglemaster:local-only-password@postgres:5432/auth_db?sslmode=disable' \
  --from-literal=MASTER_KEY=local-master-key
apply_secret flag-secret \
  --from-literal='DATABASE_URL=postgres://togglemaster:local-only-password@postgres:5432/flag_db?sslmode=disable'
apply_secret targeting-secret \
  --from-literal='DATABASE_URL=postgres://togglemaster:local-only-password@postgres:5432/targeting_db?sslmode=disable'
apply_secret evaluation-secret --from-literal=SERVICE_API_KEY=local-service-key
apply_secret analytics-secret --from-literal=LOCAL_ONLY=true
kubectl --context "$context" apply -k local/kubernetes
