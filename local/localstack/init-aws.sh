#!/bin/bash
# Executado pelo LocalStack quando fica pronto: recria a fila SQS e a tabela DynamoDB do Terraform.
set -euo pipefail
awslocal sqs create-queue --queue-name togglemaster-evaluation-events
awslocal dynamodb create-table --table-name ToggleMasterAnalytics \
  --attribute-definitions AttributeName=event_id,AttributeType=S \
  --key-schema AttributeName=event_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
