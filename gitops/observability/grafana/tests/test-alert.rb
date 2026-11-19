require "yaml"
require "tmpdir"
require "fileutils"

root = File.expand_path("..", __dir__)
names = {
  "togglemaster-auth-5xx" => "auth:5xx_ratio",
  "togglemaster-http-5xx" => "http:5xx_ratio",
  "togglemaster-http-latency" => "http:slow_ratio"
}
alerts = Dir[File.join(root, "alerts", "*.yaml")].flat_map do |path|
  YAML.load_file(path).fetch("groups").flat_map { |group| group.fetch("rules") }
end
abort "Conjunto de alertas inesperado" unless alerts.map { |a| a.fetch("uid") }.sort == names.keys.sort
generated = alerts.flat_map do |alert|
  data = alert.fetch("data").each_with_object({}) { |item, hash| hash[item.fetch("refId")] = item }
  model = data.fetch("A").fetch("model")
  threshold = data.fetch("C").fetch("model").fetch("conditions").first.fetch("evaluator")
  abort "Condição Grafana inesperada" unless alert.fetch("condition") == "C" &&
    alert.fetch("noDataState") == "NoData" && alert.fetch("execErrState") == "Error" &&
    model.fetch("instant") && data.fetch("B").fetch("model").fetch("reducer") == "last" &&
    data.fetch("B").fetch("model").fetch("expression") == "A" &&
    data.fetch("C").fetch("model").fetch("expression") == "B" && threshold.fetch("type") == "gt"
  name = names.fetch(alert.fetch("uid"))
  [
    {"record" => name, "expr" => model.fetch("expr")},
    {"alert" => alert.fetch("uid"), "expr" => "#{name} > #{threshold.fetch("params").first}",
     "for" => alert.fetch("for"), "labels" => alert.fetch("labels")}
  ]
end
rules = {
  "groups" => [{
    "name" => "test",
    "interval" => "30s",
    "rules" => generated
  }]
}

Dir.mktmpdir("toggle-alert-", __dir__) do |dir|
  File.chmod(0o755, dir)
  File.write(File.join(dir, "rules.yml"), YAML.dump(rules))
  Dir[File.join(__dir__, "*.test.yml")].each { |path| FileUtils.cp(path, dir) }
  success = system(
    "docker", "run", "--rm", "--network", "none",
    "--pull", "never",
    "-v", "#{dir}:/rules:ro", "--entrypoint", "promtool",
    "prom/prometheus:v2.54.1", "test", "rules",
    *Dir[File.join(dir, "*.test.yml")].map { |path| "/rules/#{File.basename(path)}" }
  )
  abort "Falha nos testes PromQL" unless success
end
