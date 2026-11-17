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
