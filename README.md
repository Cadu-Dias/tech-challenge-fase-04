# ToggleMaster — Fase 04 em implementação

> **Entrega incompleta:** os cinco serviços foram sincronizados no EKS e o
> fluxo sintético até DynamoDB foi verificado. A stack de observabilidade no
> EKS, integrações externas e prova de self-healing ainda não foram comprovadas.
> O ambiente local e o responder experimental não substituem essas evidências.
> As instruções
> históricas abaixo descrevem a fase 03 e **não autorizam sua execução** na
> fase 04.
>
> A fase 04 usará uma conta de laboratório distinta e infraestrutura nova,
> sem importar ou migrar o state da fase 03. O novo state deve ter um único
> dono operacional; confirme a conta, o inventário, os custos e o plano antes
> de criar recursos. Não execute bootstrap, apply, destroy, import ou alterações
> de state sem autorização específica para cada operação.
> Não use `untaint` ou `-refresh=false` para contornar a restrição do bootstrap.
> O overlay `prod` é um nome herdado do laboratório, não produção real.
>
> Publicação de imagens e atualização GitOps via CI estão desabilitadas por
> padrão. A variável de repositório `ENABLE_IMAGE_PUBLISH=true` só deve ser
> configurada após autorização explícita para o destino e publicação.

## Base herdada da fase 03

Continuação do [ToggleMaster Fase 2](https://github.com/Cadu-Dias/tech-challenge-fase-02):
os mesmos 5 microsserviços (auth, flag, targeting, evaluation, analytics), agora com:

1. **Infraestrutura 100% em Terraform** (com backend remoto S3), substituindo o
   provisionamento manual via AWS CLI da Fase 2.
2. **Pipelines de CI com DevSecOps bloqueante** (GitHub Actions).
3. **CD via GitOps com ArgoCD**, substituindo o `kubectl apply` manual.

---

## 📁 Estrutura do repositório

```
.
├── services/                 # os 5 microsserviços (código-fonte + testes + Dockerfile)
│   ├── auth-service/          (Go)
│   ├── flag-service/          (Python/Flask)
│   ├── targeting-service/     (Python/Flask)
│   ├── evaluation-service/    (Go)
│   └── analytics-service/     (Python/Flask, worker)
├── infra/                    # Terraform: toda a infra AWS + instalação do ArgoCD/ingress-nginx
│   ├── bootstrap/              stack separada: bucket S3 do state remoto
│   └── modules/                network, eks, rds, elasticache, dynamodb, sqs, ecr,
│                                secrets, argocd, ingress_nginx
├── gitops/                   # manifestos Kubernetes (Kustomize), sincronizados pelo ArgoCD
│   ├── base/                   um diretório por recurso/serviço
│   └── overlays/prod/          overlay único usado em produção
└── .github/
    ├── workflows/             5 pipelines de CI (um por serviço) + terraform.yml
    └── actions/                composite actions reutilizáveis (trivy scan, gitops-bump)
```

---

## 🏗️ Arquitetura

```
GitHub Actions (CI)                    ArgoCD (CD/GitOps)
────────────────────                   ───────────────────
build → test → lint → security   push   watch gitops/overlays/prod
  → docker build/scan → push ECR ────▶  sync automático (prune + self-heal)
  → bump da tag no gitops/                    │
                                              ▼
                                   ┌─────────────────────────────┐
                     Internet ───▶ │  ingress-nginx + NLB         │
                                   └──────────────┬──────────────┘
        ┌───────────────┬───────────────┬─────────┴─────┬────────────────┐
        ▼               ▼               ▼               ▼                ▼
 ┌────────────┐  ┌────────────┐  ┌─────────────┐  ┌────────────┐  ┌──────────────┐
 │ auth-svc   │  │ flag-svc   │  │ targeting   │  │ evaluation │  │ analytics    │
 └─────┬──────┘  └─────┬──────┘  └──────┬──────┘  └─────┬──────┘  └──────┬───────┘
   ┌───▼───┐       ┌───▼───┐        ┌───▼───┐      ┌────▼────┐      ┌─────▼─────┐
   │  RDS  │       │  RDS  │        │  RDS  │      │ Redis   │      │ DynamoDB  │
   └───────┘       └───────┘        └───────┘      └────┬────┘      └─────▲─────┘
                                                        ▼                │
                                                   ┌─────────┐           │
                                                   │  SQS    │───────────┘
                                                   └─────────┘
```

Todos os recursos AWS (VPC, EKS, RDS, ElastiCache, DynamoDB, SQS, ECR, Secrets Manager)
são criados pelo Terraform em `infra/`. O ArgoCD e o ingress-nginx são instalados pelo
próprio Terraform (provider `helm`), mas os *workloads* dos 5 microsserviços são geridos
via GitOps (a partir de `gitops/overlays/prod`).

---

## ☁️ Restrições do AWS Academy (Learner Lab) que moldaram o desenho

- **Proibido criar IAM Roles/Policies** → EKS (cluster role + node group role) reusam a
  role pré-existente `LabRole` via `data "aws_iam_role" "lab_role"`. Sem IRSA — os pods
  usam as credenciais do nó via IMDS, como na Fase 2.
- **IMDS hop limit** → o launch template do node group define
  `metadata_options.http_put_response_hop_limit = 2` (bug conhecido da Fase 2: com o
  padrão 1, os pods não alcançam o IMDS para pegar as credenciais da `LabRole`).
- **Sem OIDC GitHub→AWS** (exigiria criar uma IAM Role) → os workflows usam as
  credenciais temporárias do Lab guardadas como *secrets* do repositório
  (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`), que precisam ser
  atualizadas a cada nova sessão do Lab (elas expiram em poucas horas).
- **SCP explícita nega `s3:GetBucketObjectLockConfiguration`** — isso quebra o ciclo
  padrão Create+Read do recurso `aws_s3_bucket` usado no bucket do state remoto
  (`infra/bootstrap`). Ver seção *Runbook* abaixo para o workaround.

---

## 🚀 Runbook — provisionando a infraestrutura do zero

### 1. Pré-requisitos

- Terraform >= 1.10, AWS CLI, `kubectl`.
- Credenciais do AWS Academy Learner Lab em `~/.aws/credentials` (perfil `default`).

### 2. Backend remoto e bootstrap

No laboratório, o bootstrap Terraform criou o bucket mas falhou na leitura de
Object Lock, negada por SCP, deixando o recurso `tainted` no state local. O
bucket foi configurado por AWS CLI, sem remover ou alterar esse state. Preserve
o state local fora do Git e não repita o apply do bootstrap, nem migre, importe
ou remova state sem um plano e autorização específicos. A stack principal usa
um backend S3 novo e independente da fase 03.

### 3. Stack principal (rede, EKS, dados, ArgoCD, ingress)

O apply autorizado criou a infraestrutura e instalou ArgoCD e ingress-nginx.
Antes de qualquer alteração posterior, confirme conta e backend, gere um novo
plano e revise alterações e custos; não reaplique planos antigos após falhas.
Os outputs identificam os serviços criados, mas podem conter dados sensíveis.

### 4. Build e push das imagens (primeira vez, antes do primeiro sync do ArgoCD)

As cinco imagens iniciais foram publicadas no ECR do laboratório com a tag
imutável `phase04-initial`. O Application recebe os registros dos outputs
Terraform; as tags ficam nos kustomizations de cada serviço.

### 5. Validar

O Application ficou `Synced/Healthy` e os cinco deployments disponíveis. Um
teste sintético verificou auth, flag, targeting, evaluation, SQS, analytics e
DynamoDB via port-forward local e removeu os dados criados. O overlay não
habilita os Ingresses HTTP herdados; a exposição externa depende de TLS e
revisão específicos.

### 6. Destruir (evitar consumo de créditos do Lab)

Destruição de infraestrutura ou do bucket de state exige revisão e autorização
específicas. O EKS 1.31 está em suporte estendido até 26/11/2026; o preço público
consultado para o plano de controle nesse regime é US$ 0,60/h, além dos demais
recursos. Consulte [versões EKS](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)
e [preços EKS](https://aws.amazon.com/eks/pricing/) antes de planejar upgrade ou custos.

---

## 🔐 Segredos

Nenhuma credencial é commitada. O módulo `infra/modules/secrets`:

1. Gera senhas (`random_password`) para os 3 bancos RDS, a `MASTER_KEY` do auth-service
   e a `SERVICE_API_KEY` compartilhada entre os serviços internos.
2. Persiste cada payload em **AWS Secrets Manager** (`togglemaster/<env>/<serviço>`),
   para auditoria/rotação.
3. Materializa os mesmos valores como **Kubernetes Secrets** (`auth-secret`,
   `flag-secret`, `targeting-secret`, `evaluation-secret`) via provider `kubernetes`,
   consumidos pelos Deployments com `envFrom.secretRef` — substituindo os antigos
   `secret.template.yaml` preenchidos manualmente na Fase 2.

Os workflows de CI usam as credenciais do Lab como *secrets* do repositório GitHub
(`Settings → Secrets and variables → Actions`): `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `TF_STATE_BUCKET`.

---

## 🔁 Fluxo de CI/CD (GitOps)

Cada serviço tem seu próprio workflow (`.github/workflows/ci-<serviço>.yml`), disparado
por `pull_request`/`push` filtrado por `paths: services/<serviço>/**`:

1. **build-test** — `go test`/`pytest` com cobertura.
2. **lint** — `golangci-lint` (Go) ou `flake8` (Python).
3. **security** — Trivy *filesystem scan* (SCA/dependências) + `gosec`/`bandit` (SAST).
   Achados `CRITICAL` **bloqueiam** os jobs seguintes.
4. **docker** *(só em push na `main`)* — build da imagem, Trivy *image scan*
   (`CRITICAL` bloqueia o push), login no ECR e push com tag `v1.0.0-<sha7>`.
   O job depende de `ENABLE_IMAGE_PUBLISH=true`.
5. **gitops-bump** — `kustomize edit set image` na base do serviço, commit com
   `[skip ci]` e push na `main`.

O ArgoCD (instalado pelo Terraform, `sync automático` com `prune: true, selfHeal: true`)
detecta o novo commit em `gitops/overlays/prod` e aplica a mudança no cluster — nenhum
`kubectl apply` manual é necessário.

Workflow `terraform.yml`: `fmt`/`validate`/`tfsec`/`plan` em todo PR que toque `infra/`;
`apply` disponível via `workflow_dispatch` (aprovação manual, ambiente `production`).

---

## 🧪 Testes

- **auth-service** / **evaluation-service** (Go): `go test ./... -cover`
  (`key_test.go`, `evaluator_test.go`).
- **flag-service** / **targeting-service** / **analytics-service** (Python):
  `pytest` com mocks de banco (`psycopg2.pool`) e AWS (`boto3.Session`,
  `threading.Thread`), sem dependências externas reais.

```bash
# Go
cd services/auth-service && go test ./... -cover

# Python
cd services/flag-service && pip install -r requirements.txt -r requirements-dev.txt && pytest
```

---

## 📌 O que muda em relação à Fase 2

| Tema | Fase 2 | Fase 3 |
|---|---|---|
| Infraestrutura | AWS CLI manual | Terraform (backend S3, módulos reutilizáveis) |
| Deploy | `kubectl apply -f` manual | ArgoCD (GitOps), sync automático |
| Segredos | `secret.template.yaml` preenchido à mão | Terraform → Secrets Manager → K8s Secret |
| Qualidade | Nenhum teste/lint | Testes unitários + lint bloqueante no CI |
| Segurança | Nenhuma verificação | Trivy (SCA+imagem), gosec, bandit, tfsec bloqueantes |
| Imagens | Push manual (`docker push`) | CI builda, escaneia e publica no ECR |

---

## 🎬 Roteiro do vídeo de apresentação

Ver [`docs/ROTEIRO-VIDEO.md`](docs/ROTEIRO-VIDEO.md) para o roteiro completo
(até 20 minutos) usado na gravação da demonstração.

