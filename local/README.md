# Ambiente local

Execute `docker compose -f local/docker-compose.yml up --build` com Docker Compose.
Os serviços publicados escutam somente em `127.0.0.1`. O Grafana exige login
`admin` e a senha sintética `local-only-grafana`, substituível por
`LOCAL_GRAFANA_PASSWORD`. Todas as demais credenciais padrão também são
exclusivamente locais; não reutilize credenciais reais.

O PostgreSQL inicializa novos volumes com SCRAM-SHA-256. Volumes previamente
inicializados com MD5 não são convertidos automaticamente; preserve seus dados
e migre usuários e configuração de autenticação antes de reutilizá-los.

O Promtail monta o socket Docker para descobrir logs. Mesmo montado como somente
leitura, o socket permite controle privilegiado do daemon: use este ambiente
apenas em máquina e projetos confiáveis. Não o exponha em uma rede compartilhada.
Os logs passam pelo receptor Loki do Collector (alpha na versão local), são
agrupados por serviço e exportados via OTLP HTTP ao Loki. Collector e Promtail
são excluídos da coleta para evitar recursão. Esse fluxo local não configura
envio externo; logs legados ainda precisam de revisão antes do uso com Datadog.
LocalStack, Grafana e demais componentes locais não comprovam execução no EKS
ou entrega a Datadog, PagerDuty e Teams.

## Kubernetes local

O overlay `kubernetes/` é exclusivo para um cluster kind descartável chamado
`toggle-observability`, com namespaces `toggle` e `observability`. Exige
`kubectl`, Docker, kind, Ruby, OpenSSL e um kubeconfig dedicado apontando para a API em
`127.0.0.1`; o script recusa outros contextos e servidores. Use apenas dados
sintéticos e credenciais locais para os serviços. As credenciais de integração
Datadog, PagerDuty e Teams, se configuradas, são exceções: mantenha-as em
Secrets locais, nunca no repositório, argumentos ou histórico do shell.
PostgreSQL, Redis, LocalStack,
Grafana, Prometheus, Loki e Tempo usam armazenamento efêmero; reiniciar seus
pods pode apagar dados. Não use esta configuração para produção.

### Gerar HTTP 500 contínuos para demonstração

Para encaminhar auth 5xx, HTTP 5xx e latência HTTP a **PagerDuty e Telegram**,
com autorização de envio aos canais existentes, execute
`python3 local/kubernetes/configure-auth-http-notifications.py`.
O comando exige os contact points `local-pagerduty` e `Telegram`, acrescenta
rotas exatas antes das existentes e preserva self-healing e destinos anteriores.
Salva o provisionamento no ConfigMap e recarrega via API administrativa, sem
reiniciar Grafana nem enviar teste sintético. Alertas já ativos podem notificar
imediatamente. O `apply.sh` preserva esse arquivo de políticas nas próximas
reconciliações; um cluster novo exige configurar novamente.

Atalho, com `KUBECONFIG` local já configurado: `sh local/kubernetes/gerar-500.sh`.
Gera até duas chamadas por segundo durante cinco minutos, usando somente as
rotas existentes. Pode disparar notificações externas; execute apenas com
autorização. `Ctrl+C` interrompe.

`python3 local/kubernetes/generate-auth-500.py` faz somente a pré-verificação,
sem enviar erros nem notificações. Exige `KUBECONFIG` local; não condiciona a
geração ao destino da notificação. `--require-pagerduty` exige adicionalmente uma
rota efetiva do alerta `Auth com taxa elevada de erros 5xx` para `local-pagerduty`;
apenas ter o contact point não basta. Caso ausente, configure uma rota com os matchers
exatos `alertname=Auth com taxa elevada de erros 5xx`, `namespace=toggle` e
`service=auth-service`, antes das rotas auth existentes e com continuação
habilitada, preservando seus destinos. O script não modifica políticas.

Somente após autorizar a execução e todos os destinos exibidos na pré-verificação:

```sh
python3 local/kubernetes/generate-auth-500.py \
  --run --allow-external-notifications --duration 300 --interval 1
```

Envia uma requisição por segundo, por cinco minutos (limite de 15 minutos).
`Ctrl+C` interrompe. Usa o mesmo nome sintético maior que `VARCHAR(100)` da
prova existente para provocar 500 em `/admin/keys`, conferindo antes o limite
no banco; não insere chaves, altera pods ou habilita falha opt-in.
A causa é entrada inválida, não indisponibilidade espontânea do banco.
As credenciais são lidas dos Secrets locais, sem impressão.

O alerta exige mais de 5% de erros e ao menos 20 requisições na janela de
dois minutos, mais um minuto em Pending. O responder pode reiniciar o auth;
se isso derrubar o port-forward, o script avisa e tenta restabelecê-lo até
três vezes consecutivas, sem reiniciar a duração da prova. Uma indisponibilidade
persistente interrompe a geração com erro. Todos os destinos ativos podem notificar (incluindo o canal
anterior), conforme a política existente. Não envia eventos diretamente ao
PagerDuty nem garante entrega: confira Firing/Normal no Grafana e o incidente
no PagerDuty. Após parar, aguarde a janela de métricas e a resolução; não
desabilite a regra nem apague deduplicação/cooldown para forçar novo incidente.

Com o cluster e namespaces já criados e `KUBECONFIG` apontando para ele,
prepare o Secret `observability/grafana-admin` com as chaves `username` e
`password`, sem expor seus valores. Gere as credenciais sintéticas de telemetria
uma única vez com `sh local/kubernetes/setup-telemetry.sh`; o script não
substitui Secrets existentes. Gere também o token isolado das métricas auth
com `sh local/kubernetes/setup-auth-metrics.sh`. Em seguida:

```sh
for s in auth-service flag-service targeting-service evaluation-service analytics-service; do
  docker build -t "togglemaster-kind/$s:local" "services/$s"
done
kind load docker-image --name toggle-observability \
  togglemaster-kind/auth-service:local togglemaster-kind/flag-service:local \
  togglemaster-kind/targeting-service:local togglemaster-kind/evaluation-service:local \
  togglemaster-kind/analytics-service:local postgres:16-alpine redis:7-alpine \
  localstack/localstack:3.8 otel/opentelemetry-collector-contrib:0.111.0 \
  otel/opentelemetry-collector-contrib:0.161.0 prom/prometheus:v2.54.1 \
  grafana/loki:3.2.0 grafana/tempo:2.6.1 grafana/grafana:11.2.2 \
  registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0 python:3.11-slim
sh local/kubernetes/apply.sh
```

As imagens precisam existir previamente no Docker; o overlay não faz pull.
O `apply.sh` reconcilia a configuração básica do Collector e pode desligar o
pipeline opcional Datadog; para retomá-lo, execute novamente o gate de ativação.
Para instalar o responder pela primeira vez, carregue também no kind a imagem
`toggle/self-healing-responder:phase04-initial` construída a partir de
`automation/self-healing`, execute `sh local/kubernetes/setup-self-healing.sh`
e depois `sh local/kubernetes/apply.sh`. O setup recusa substituir recursos
existentes ou apagar o estado de incidentes.
Em terminais separados, os seguintes encaminhamentos publicam apenas em loopback:

```sh
kubectl --context kind-toggle-observability -n observability port-forward --address 127.0.0.1 svc/grafana 13000:3000
kubectl --context kind-toggle-observability -n toggle port-forward --address 127.0.0.1 svc/evaluation-service 18004:8004
```

Os processos de `port-forward` independentes podem continuar após o encerramento
da sessão do assistente, mas terminam quando o pod atendido é substituído ou o
processo é interrompido; nesse caso, execute novamente os comandos acima. O
cluster depende de Docker em execução e não há garantia de retomada automática
após desligar a máquina ou o Docker. Guarde o kubeconfig local em local seguro
fora dos arquivos temporários da sessão; ele contém credenciais do cluster.
Volumes efêmeros não garantem preservação dos dados entre substituições de pods.

Grafana: <http://127.0.0.1:13000>. O Secret `grafana-admin` no namespace
`observability` fornece `username` e `password`; não o versione nem
coloque seus valores em comandos ou histórico do shell. Não exponha os
encaminhamentos na rede. O agente descobre arquivos de logs de pods de todos os
namespaces do nó kind, exceto os próprios logs, com montagem somente leitura
dos logs do nó e execução como root. Encaminha ao Loki local apenas eventos JSON
`http.request` e `sqs.process`, com campos e valores restritos; linhas não
estruturadas e demais eventos são descartados. O teste observou dois namespaces
e quatro contêineres, não todas as fontes nem todas as linhas do cluster.
Identificadores de trace e span só são preservados para os cinco serviços em
`toggle`. Nomes Kubernetes e IDs válidos ainda podem conter dados sensíveis:
use somente dados sintéticos e nomes não sensíveis neste laboratório. Os filtros
não garantem prevenção completa de vazamentos nem são política de produção.

Os painéis de CPU, memória, réplicas e reinícios usam o cAdvisor do kubelet e
o kube-state-metrics local. O Prometheus autentica o scrape HTTPS do cAdvisor
com uma ServiceAccount autorizada somente a `get nodes/metrics` no nó kind;
apenas métricas de CPU e memória do namespace `toggle` são retidas. O
kube-state-metrics possui acesso de leitura somente a pods e deployments de
`toggle`. Ambos são exclusivos deste cluster de nó único e não demonstram
coleta no EKS.

O certificado público do kubelet é fixado como âncora de confiança após
validar seu nome e validade; a extração pressupõe um host Docker confiável.
Após rotação do certificado do kubelet ou alteração do IP do nó, reaplique a
coleta e reinicie o Prometheus com o comando abaixo. Ele não reconcilia o
Collector nem altera o pipeline Datadog, mas interrompe temporariamente as
consultas ao Prometheus:

```sh
sh local/kubernetes/apply-metrics.sh
python3 local/kubernetes/smoke-kubernetes-metrics.py
```

O teste verifica as consultas dos quatro painéis pela API do Grafana e
permissões positivas e negativas. Exige o Secret local `grafana-admin` e a
imagem `python:3.11-slim` carregada no kind para um pod temporário de teste.

O Collector usa TLS e token para receber OTLP; os serviços assinam os contextos
HTTP e SQS com HMAC e descartam contextos não autenticados. O serviço de
avaliação inicia um trace novo. Sem chave HMAC, os serviços preservam o
comportamento anterior e a exportação externa não é segura. O token e a chave
são compartilhados: um workload confiável comprometido pode forjar telemetria.
Os certificados sintéticos da telemetria e do webhook expiram em sete dias.
Antes do vencimento, com `KUBECONFIG` local definido, execute:

```sh
python3 local/kubernetes/rotate-certificates.py
```

O procedimento verifica os recursos, cria certificados novos, atualiza os
Secrets por referência e reinicia Collector, agente, serviços, responder e
Grafana. Se o Datadog estiver ativo, desliga-o durante a troca e tenta reativá-lo
somente após os testes do gate. Há interrupção local temporária. Se falhar
durante a atualização, mantenha o Datadog desligado, corrija a causa e repita o
procedimento inteiro; depois verifique os testes abaixo antes de reativá-lo
manualmente. Não copie nem imprima as chaves privadas.

O Collector sempre envia traces ao Tempo local. A exportação opcional ao Datadog
exige `observability/datadog-credentials`, com chaves `api-key` e `site`, e
autorização específica para envio. O pipeline permite apenas serviços e
operações conhecidos, descarta spans com eventos ou links e remove atributos,
tracestate, mensagens de status e URLs de schema. Esse filtro não oferece
identidade individual nem proteção absoluta contra emissores confiáveis
comprometidos. Para ativar no cluster local autorizado:

```sh
sh local/kubernetes/enable-datadog.sh
```

O script executa verificações locais antes e depois da ativação e tenta desligar
o pipeline se uma delas falhar; uma falha no desligamento exige intervenção.
Para interromper o envio sem excluir o Secret:

```sh
sh local/kubernetes/disable-datadog.sh
```

`python3 local/kubernetes/smoke-datadog.py` exercita os cinco serviços com
dados sintéticos e verifica o contador de spans enviados pelo exportador.
Esse contador não comprova visualização ou retenção no APM Datadog.
Em 17/11/2026, o usuário forneceu uma captura do APM com spans dos cinco
serviços. A lista não comprova a árvore pai-filho, o Service Map nem a
retenção.

Quando o Secret `observability/notification-credentials` contém as chaves
`pagerduty-key` e `teams-webhook`, o Grafana provisiona os contact points
correspondentes, mas não há rotas para eles. Em 17/11/2026, um teste sintético
autorizado pelo usuário foi aceito pelo webhook Teams via Grafana. O usuário
inicialmente informou que a notificação do workflow ainda não havia aparecido;
posteriormente, informou que o Teams funcionou, sem detalhar qual mensagem ou
execução observou. A captura e a confirmação específica do teste permanecem
pendentes. O contact point usa o mesmo webhook do Secret e um
teste isolado confirmou o formato Adaptive Card. O teste sintético
`TC04-TEAMS-1`, enviado em 17/11/2026 às 08:24:47 no horário local, também
recebeu aceite HTTP. Esse aceite, por si só, não confirma o processamento nem
a entrega da mensagem específica.
O teste PagerDuty recebeu HTTP 400 para trigger e
resolução com a chave anterior. Após sua correção, a Events API aceitou o
trigger e a resolução do mesmo evento sintético. O aceite é assíncrono e não
comprova, por si só, a criação ou o estado final do incidente no PagerDuty.
Também foi testado o contact point provisionado do Grafana, com campos
sintéticos e sem roteamento persistente: o Grafana confirmou o envio e a Events
API aceitou a resolução com a chave de deduplicação correspondente. O usuário
forneceu posteriormente uma captura do PagerDuty mostrando um alerta sintético
ToggleMaster com status `Resolved`. A captura comprova o estado desse alerta,
não o estado final de um incidente.
Não ative ou teste
notificações externas sem autorização. O comando
`python3 local/kubernetes/test-contactpoints-isolated.py` testa os tipos de
notificação Grafana contra mocks locais, com credenciais sintéticas e rede
Docker isolada; não valida as credenciais nem a entrega externa.

Com o responder instalado, o Grafana encaminha somente o alerta auth crítico
local ao seu webhook TLS autenticado. O Prometheus consulta um receptor de
métricas separado, com token exclusivo do auth; a regra local filtra o job
`auth-metrics`, em vez de confiar apenas no rótulo estático de namespace.
Esse isolamento depende da proteção do token e do cluster; um workload que
obtenha o token dedicado pode forjar métricas. A NetworkPolicy depende de uma
CNI que a implemente e não substitui autenticação.

Os seguintes testes usam apenas o cluster local e dados sintéticos:

```sh
python3 local/kubernetes/test-local-auth-alert.py
python3 local/kubernetes/smoke-auth-metrics.py
python3 local/kubernetes/smoke-grafana-responder.py
python3 local/kubernetes/test-log-sanitize.py
python3 local/kubernetes/smoke-logs.py
```

`smoke-grafana-responder.py` cria pods temporários e envia métricas sintéticas de erro,
sem ativar a falha no serviço; isso pode causar um rollout real **local** do
auth. Ele deve executar sem outro incidente auth ativo e remove seus pods ao
terminar. A reserva órfã do responder requer revisão manual; incidentes
recebidos durante o cooldown são descartados.

`smoke-fault-healing.py` é uma demonstração invasiva no kind descartável: habilita
temporariamente a falha opt-in de `/validate` (503), envia requisições sintéticas
com nome inválido para provocar 500 em `/admin/keys` e espera alerta e rollout.
Não representa uma falha espontânea do banco. O script verifica a política de
notificação, exige responder fora de cooldown, restaura a configuração e aguarda
a resolução. Execute somente com autorização para falha local e interrupção
temporária, sem outros testes concorrentes ou incidentes ativos.

## Roteiro e relatório

O [roteiro da fase 04](../docs/ROTEIRO-VIDEO-FASE-04.md) distingue a demonstração
local das pendências externas. Para gerar um **rascunho**, em um ambiente Python
isolado, instale `docs/requirements-relatorio.txt` e execute:

```sh
python3 docs/gerar-relatorio-fase-04.py --saida /caminho/para/relatorio-rascunho.pdf
```

`--help` mostra as opções para identificação confirmada, URL pública do vídeo e
diretório de capturas. As capturas opcionais devem ser revisadas para remover
dados sensíveis antes de sua inclusão; os nomes esperados são `arquitetura.png`,
`dashboard-cpu.png`, `dashboard-memoria.png`, `dashboard-reinicios.png`,
`dashboard-replicas.png`, `trace.png`, `apm-spans.png`, `service-map.png`, `teams.png`,
`pagerduty.png` e `self-healing.png`. Como alternativa aos quatro painéis
separados, é aceito `dashboard.png`. Capturas ausentes permanecem explicitamente pendentes.
O PDF não é a entrega final enquanto identificação, links, evidências e custos
não forem confirmados e revisados.
