# Responder de recuperação

Implementação experimental **não habilitada** no overlay nem conectada ao
Grafana. O webhook aceita somente o alerta firing permitido para auth em
`toggle`, autenticado por token em arquivo Secret e com TLS obrigatório. A reserva do incidente e o
cooldown são persistidos no ConfigMap por atualização otimista; falhas e
reservas incompletas não geram segunda tentativa. A capacidade é limitada a 500
incidentes e exige revisão operacional ao esgotar. Reinicia apenas o Deployment
permitido e aguarda rollout; não considera NoData ou resolved como recuperação.

Antes de habilitar: testar RBAC real e política de rede com o CNI, criar Secret
fora do Git, publicar imagem, configurar certificado confiável e contact point,
validar concorrência e rollout no cluster e configurar ArgoCD para ignorar
somente `/data/state` do ConfigMap e a anotação de restart do Deployment, com
`RespectIgnoreDifferences=true`. Não iniciar prova de falha ou notificações
sem a autorização específica correspondente.

Testes locais: `cd automation/self-healing && python3 -m unittest -v`.
