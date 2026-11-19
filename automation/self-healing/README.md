# Responder de recuperação local

Implementação experimental. No cluster kind local autorizado, foi conectada ao
Grafana e validada com métricas sintéticas; isso não comprova instalação ou
funcionamento no EKS. Roda dentro de um cluster Kubernetes; não controla
containers do Docker Compose diretamente. O único alvo permitido é o Deployment
`toggle/auth-service`. Não há acesso a AWS/EKS nem envio de notificações pelo
responder.

## Contrato

`POST /webhook` exige HTTPS e `Authorization: Bearer <token>`. O token é lido
de `WEBHOOK_TOKEN_FILE` na inicialização e deve ter ao menos 32 caracteres;
certificado e chave vêm de `WEBHOOK_TLS_CERT_FILE` e `WEBHOOK_TLS_KEY_FILE`.
`GET /health` é público para as sondas. Configure o Grafana para confiar no
certificado, cujo nome deve corresponder ao endereço usado no contact point.

O corpo é o JSON padrão do webhook do Grafana, com `alerts` contendo entre 1 e
20 alertas. Cada alerta firing precisa de `fingerprint` hexadecimal minúsculo
de 16 a 64 caracteres, `startsAt` ISO 8601 com fuso e labels exatos:

```json
{
  "alerts": [{
    "status": "firing",
    "fingerprint": "aaaaaaaaaaaaaaaa",
    "startsAt": "2026-11-17T00:00:00Z",
    "labels": {
      "alertname": "Auth com taxa elevada de erros 5xx",
      "service": "auth-service",
      "namespace": "toggle"
    }
  }]
}
```

O contrato de fingerprint deve ser conferido com a versão e configuração do
Grafana antes de habilitar o contact point. Alertas `resolved` são ignorados;
outros status e firing fora da allowlist são rejeitados. Todo o lote é
validado antes de qualquer ação, mas a execução de um lote não é transacional:
ações anteriores podem ter ocorrido se uma posterior falhar.

Um incidente é identificado pelo fingerprint e início normalizado em UTC. Uma
reserva no ConfigMap, usando `resourceVersion`, impede ações concorrentes,
inclusive após expirar o cooldown de 600 segundos. Após sucesso, repetição do
mesmo incidente retorna `duplicate`; outro incidente no cooldown retorna
`cooldown` sem ação e **não é agendado**. O retorno 200 contém `results` com
`restarted`, `duplicate`, `cooldown` ou `ignored`.

Falhas, reservas em andamento ou órfãs e indisponibilidade da API retornam
503. Falhas e reservas incompletas não são repetidas automaticamente; uma
reserva órfã bloqueia também novos incidentes, até revisão manual. O restart é
considerado concluído somente após confirmação do rollout. Como uma falha
pode acontecer depois da alteração no Deployment, investigue o estado real
antes de corrigir manualmente o ConfigMap `toggle-self-healing-state`; não
apague o estado às cegas. O limite é 500 incidentes, sem expiração automática.
O relógio do pod deve estar sincronizado: o cooldown usa tempo de parede.

## Integração opt-in com kubectl local

Não execute estes passos em um contexto remoto. Primeiro confirme manualmente
que o contexto, servidor, proxy e credenciais do kubeconfig pertencem ao cluster
local escolhido. Use sempre um kubeconfig e contexto locais explícitos; confira
o destino de `kubectl config view --minify` antes de qualquer comando mutável.
O ambiente local precisa ter o namespace `toggle` e o Deployment `auth-service`
com pelo menos uma réplica, além de uma imagem `toggle/self-healing-responder:phase04-initial`
construída a partir deste diretório e carregada no runtime do cluster local.

Crie fora do Git um arquivo de token e os arquivos de certificado e chave TLS,
com permissões restritas. Em seguida, **somente no contexto local verificado**:

```sh
kubectl --kubeconfig "$LOCAL_KUBECONFIG" --context "$LOCAL_CONTEXT" -n toggle create secret generic toggle-self-healing-webhook --from-file=token="$TOKEN_FILE"
kubectl --kubeconfig "$LOCAL_KUBECONFIG" --context "$LOCAL_CONTEXT" -n toggle create secret tls toggle-self-healing-tls --cert="$TLS_CERT_FILE" --key="$TLS_KEY_FILE"
kubectl --kubeconfig "$LOCAL_KUBECONFIG" --context "$LOCAL_CONTEXT" apply -f automation/self-healing/kubernetes.yaml
```

O manifesto é independente do overlay e cria RBAC limitado, Service e
NetworkPolicy de entrada para pods Grafana no namespace `observability`. A
política depende de suporte do CNI e dessas labels; Grafana no Compose ou em
outro namespace não corresponderá a ela. Ajustes de rede e de confiança TLS
dependem da topologia local. Configure o contact point somente após verificar
conectividade, TLS, credenciais e RBAC, sem desabilitar a verificação de TLS.
Não habilite notificações externas nem injete falhas em recursos compartilhados.

Se houver ArgoCD, configure-o para ignorar apenas `/data/state` do ConfigMap e
a anotação `togglemaster.io/restarted-at` do Deployment, com
`RespectIgnoreDifferences=true`, antes de habilitar o responder. Caso contrário,
um sync pode desfazer o estado ou a ação. O manifesto não instala essa
configuração nem provisiona a stack de observabilidade.

## Testes isolados

```sh
cd automation/self-healing && python3 -m unittest -v
```

Os testes usam uma API Kubernetes simulada e servidor HTTP loopback; não
reiniciam recursos nem enviam notificações. Eles não comprovam o funcionamento
de RBAC, CNI, TLS real, Grafana ou rollout num cluster local.
