# Observabilidade no laboratório EKS

`root-application.yaml` cria aplicações Argo CD separadas para os backends e
os Collectors. Não há ingress, notificações ou exportação a serviços externos.
As aplicações usam sincronização automática sem prune.

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
