# Observabilidade no laboratório EKS

`root-application.yaml` cria aplicações Argo CD separadas para os backends e
os Collectors e para o Grafana. Não há ingress, notificações ou exportação a serviços externos.
As aplicações usam sincronização automática sem prune.

Antes de publicar a aplicação Grafana, deve existir no namespace
`observability` o Secret `grafana-admin`, com as chaves não vazias `username`
e `password`, provisionado por um mecanismo seguro fora do Git. O acesso
anônimo está desabilitado e o Service é interno (`ClusterIP`); para acesso
local autenticado use `kubectl -n observability port-forward svc/grafana 3000:3000`.
O banco do Grafana usa `emptyDir`: usuários, alterações pela interface e estado
local são perdidos ao substituir o pod. O dashboard e as fontes são provisionados
pelo Git. A mudança posterior do Secret não atualiza automaticamente a senha
do administrador em um banco Grafana já inicializado.

O dashboard inclui requisições e logs dos sinais existentes. Os painéis de
CPU, memória, reinícios e réplicas dependem de métricas Kubernetes que o
Prometheus atual não coleta; ficarão sem dados até existirem fontes adequadas.

Prometheus e Loki usam `emptyDir` limitado e retenção de até 24 horas. Os dados
são perdidos na substituição dos pods; a capacidade de provisionar volumes
persistentes não foi comprovada. As métricas de consumo dos nós também não
estavam disponíveis na instalação inicial. As reservas e os limites dos pods
não garantem capacidade real de CPU, memória ou disco.

O agent lê apenas arquivos de logs do namespace `toggle`, aceita somente
eventos JSON estruturados `http.request` e `sqs.process` e mantém uma lista
limitada de campos. Isso não centraliza todos os logs de todos os contêineres
do cluster, como exige o enunciado; a ampliação depende de revisão de dados
sensíveis, exclusão de recursão e evidência de funcionamento. O acesso
read-only a `/var/log/pods` ainda expõe arquivos de outros namespaces ao
processo do agent, executado como root para ler os arquivos; o filtro de caminho
não é um limite de segurança do sistema. O preset também monta
`/var/lib/docker/containers` somente para leitura.

Os sinais trafegam sem TLS entre componentes internos do cluster. O Loki não
tem autenticação; somente um Service ClusterIP é criado. Não habilite envio
externo antes de revisar a sanitização dos três sinais e o transporte.
O gateway gera métricas de spans, mas não armazena traces: não há prova de APM
ou incidentes apenas pela instalação desta stack.
