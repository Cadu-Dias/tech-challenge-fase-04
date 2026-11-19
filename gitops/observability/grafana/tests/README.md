# Alertas HTTP: execução local e contrato

Os arquivos em `../alerts/` são provisionados pelo Compose local; não há contact
point externo configurado aqui. O manifesto Grafana do GitOps ainda **não monta
essas regras**, portanto elas não estão habilitadas no EKS. Não publicar ou
ligar notificações externas sem autorização específica.

| Regra | Sinal e condição | Rótulos relevantes |
|---|---|---|
| `togglemaster-auth-5xx` | Auth: proporção de 5xx > 5% | `service=auth-service`, `namespace=toggle`, `severity=critical` |
| `togglemaster-http-5xx` | Flag, targeting e evaluation: proporção de 5xx > 5% | `service_name`, `severity=warning` |
| `togglemaster-http-latency` | Auth, flag, targeting e evaluation: proporção de requisições > 1 s acima de 5% | `service_name`, `severity=warning` |

Cada regra usa uma janela de 2 minutos, no mínimo 20 requisições estimadas
por `increase` e condição sustentada por 1 minuto, avaliada a cada 30 segundos.
Os limites são escolhas iniciais de laboratório, não SLOs validados. O
histograma inclui exatamente 1 segundo no bucket `le="1"`; apenas valores
acima dele contam como lentos. `increase` pode extrapolar e produzir valores
fracionários: o mínimo é uma estimativa, não uma contagem exata. Com menos de
20 requisições a expressão retorna zero se houver amostras suficientes;
isso evita alertas ruidosos, mas **não demonstra saúde** nem disponibilidade.
Dados ausentes retornam NoData e erro de consulta retorna Error, não sucesso.
Sem série de 5xx, mas com contagem total, a taxa de erros é zero. Na regra de
latência, falta de bucket gera ausência de dados quando nenhum bucket existe;
ausência parcial de séries de bucket pode distorcer o resultado.

As regras usam os contadores e buckets `http_server_request_duration_seconds_*`
com `service_name` e `http_response_status_code`, provenientes dos histogramas
OTel dos quatro serviços HTTP, exportados pelo Collector e coletados pelo
Prometheus. Dependem de tráfego, exportação OTLP, preservação desses rótulos e
duas ou mais amostras por série. Não há alerta de disponibilidade: ausência
de tráfego é indistinguível de ausência de telemetria nessa métrica. Quando
uma série individual desaparece em uma consulta multidimensional, o ciclo de
vida de instâncias ausentes do Grafana não comprova recuperação; NoData aplica-se
à consulta inteira. Confirme implantação e telemetria independentemente.
Analytics é um worker; cobertura de fila, CPU, memória, reinícios e réplicas
exige sinais e fontes adicionais, não disponíveis ou comprovados aqui.

As regras GitOps e Compose agregam por nome de serviço, não por ambiente ou
namespace. O `namespace=toggle` no alerta auth é um rótulo estático, **não**
um filtro de origem. No overlay kind, a regra é gerada com filtro
`job="auth-metrics"` e usa um receptor com token dedicado; um emissor que obtenha
esse token pode forjar métricas. Esse isolamento não se aplica automaticamente
ao GitOps nem ao Compose.

## Testes locais

Com Docker disponível e a imagem `prom/prometheus:v2.54.1` já presente:

```sh
ruby gitops/observability/grafana/tests/test-alert.rb
```

O teste extrai as expressões, limiares e duração dos arquivos Grafana e executa
`promtool` em container sem rede e sem pull. Exercita ausência e baixo tráfego,
limites, serviços distintos, incidente sustentado e recuperação. A simulação
Prometheus não reproduz todos os estados ou o ciclo de vida do Grafana, nem
comprova entrega de notificações ou execução do self-healing.

## Integração proposta com self-healing

O responder experimental aceita apenas `firing` do alerta com
`alertname=Auth com taxa elevada de erros 5xx`, `service=auth-service` e
`namespace=toggle`, além de fingerprint e `startsAt`. O nome, UID e rótulos
da regra auth existente foram preservados; as regras warning não acionam o
responder. Estados `resolved`, NoData e Error não devem reiniciar serviços.
No kind local, o fluxo Grafana → webhook TLS/token → rollout foi exercitado com
métricas sintéticas e o alerta retornou ao estado resolvido. Isso não comprova
isolamento ou integração em outros ambientes; consulte `local/README.md` e
`automation/self-healing/README.md`.
