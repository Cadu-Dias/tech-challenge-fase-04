require "ipaddr"
require "json"
require "yaml"

config = YAML.load_file(File.join(__dir__, "prometheus.yaml"))
node = JSON.parse(STDIN.read)
name = node.fetch("metadata").fetch("name")
abort "Nó kind local inesperado" unless name == "toggle-observability-control-plane"
address = node.fetch("status").fetch("addresses").find { |a| a["type"] == "InternalIP" }.fetch("address")
ip = IPAddr.new(address)
abort "Endereço de nó inesperado" unless ip.ipv4? && ip.private?
job = config.fetch("scrape_configs").find { |entry| entry["job_name"] == "kubelet-cadvisor" }
job.fetch("static_configs").first["targets"] = ["#{address}:10250"]
puts YAML.dump(config)
