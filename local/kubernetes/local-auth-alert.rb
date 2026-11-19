require "yaml"

path = File.expand_path("../../gitops/observability/grafana/alerts/auth-5xx.yaml", __dir__)
document = YAML.load_file(path)
rule = document.fetch("groups").first.fetch("rules").first
abort "Regra auth inesperada" unless rule.fetch("uid") == "togglemaster-auth-5xx"
model = rule.fetch("data").find { |item| item.fetch("refId") == "A" }.fetch("model")
selector = 'service_name="auth-service"'
expression = model.fetch("expr")
abort "Seletores auth inesperados" unless expression.scan(selector).length == 3
model["expr"] = expression.gsub(selector, "#{selector},job=\"auth-metrics\"")
puts YAML.dump(document)
