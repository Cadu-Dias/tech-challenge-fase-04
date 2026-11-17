require "yaml"
require "tmpdir"
require "fileutils"

root = File.expand_path("..", __dir__)
alert = YAML.load_file(File.join(root, "alerts", "auth-5xx.yaml"))
expression = alert.fetch("groups").first.fetch("rules").first.fetch("data").first.fetch("model").fetch("expr")
rules = {
  "groups" => [{
    "name" => "test",
    "interval" => "30s",
    "rules" => [{"record" => "auth:5xx_ratio", "expr" => expression}]
  }]
}

Dir.mktmpdir("toggle-alert-", __dir__) do |dir|
  File.chmod(0o755, dir)
  File.write(File.join(dir, "rules.yml"), YAML.dump(rules))
  FileUtils.cp(File.join(__dir__, "auth-5xx.test.yml"), dir)
  success = system(
    "docker", "run", "--rm", "--network", "none",
    "-v", "#{dir}:/rules:ro", "--entrypoint", "promtool",
    "prom/prometheus:v2.54.1", "test", "rules", "/rules/auth-5xx.test.yml"
  )
  abort "Falha nos testes PromQL" unless success
end
