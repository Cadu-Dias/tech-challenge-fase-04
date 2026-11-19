import os
import sys
import hashlib
import hmac
import threading
import json
import re
import uuid
import time
import logging
import atexit
import boto3
from botocore.exceptions import NoCredentialsError, ClientError
from flask import Flask, jsonify
from dotenv import load_dotenv
from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# Configura o logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)
event_log = logging.getLogger("togglemaster.events")
event_log.setLevel(logging.INFO)
event_log.propagate = False
if not event_log.handlers:
    event_log.addHandler(logging.StreamHandler())

# Carrega .env para desenvolvimento local
load_dotenv()

trace_context_key_file = os.getenv("TRACE_CONTEXT_HMAC_KEY_FILE")
trace_context_key = None
if trace_context_key_file:
    with open(trace_context_key_file, "rb") as file:
        trace_context_key = file.read(1025).strip()
    if not 32 <= len(trace_context_key) <= 1024:
        raise ValueError("chave de contexto de trace deve ter entre 32 e 1024 bytes")

if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource({"service.name": "analytics-service"}))
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(timeout=2),
        max_queue_size=512,
        schedule_delay_millis=1000,
        export_timeout_millis=2000,
    ))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)
else:
    log.info("OpenTelemetry: endpoint não configurado; exportação de traces desabilitada")

if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(timeout=2), export_interval_millis=10000)
    meter_provider = MeterProvider(
        resource=Resource({"service.name": "analytics-service"}), metric_readers=[reader]
    )
    metrics.set_meter_provider(meter_provider)
    atexit.register(meter_provider.shutdown)
else:
    log.info("OpenTelemetry: endpoint não configurado; exportação de métricas desabilitada")

processed_messages = metrics.get_meter("togglemaster.sqs").create_counter("togglemaster.sqs.processed")

# --- Configuração ---
AWS_REGION = os.getenv("AWS_REGION")
SQS_QUEUE_URL = os.getenv("AWS_SQS_URL")
DYNAMODB_TABLE_NAME = os.getenv("AWS_DYNAMODB_TABLE")
# Número de threads consumidoras da fila. Aumentar o paralelismo faz o consumo
# da SQS refletir em uso de CPU, permitindo que o HPA por CPU escale sob carga.
WORKER_THREADS = int(os.getenv("WORKER_THREADS", "4"))

if not all([AWS_REGION, SQS_QUEUE_URL, DYNAMODB_TABLE_NAME]):
    log.critical("Erro: AWS_REGION, AWS_SQS_URL, e AWS_DYNAMODB_TABLE devem ser definidos.")
    sys.exit(1)

# --- Clientes Boto3 ---
# Criamos a sessão uma vez
try:
    session = boto3.Session(region_name=AWS_REGION)
    sqs_client = session.client("sqs")
    dynamodb_client = session.client("dynamodb")
    log.info(f"Clientes Boto3 inicializados na região {AWS_REGION}")
except NoCredentialsError:
    log.critical("Credenciais da AWS não encontradas. Verifique seu ambiente.")
    sys.exit(1)
except Exception as e:
    log.critical(f"Erro ao inicializar o Boto3: {e}")
    sys.exit(1)


# --- SQS Worker ---

def process_message(message):
    """Extrai apenas o contexto W3C, isolado por mensagem."""
    attrs = message.get("MessageAttributes", {})
    carrier = {
        key: attrs[key]["StringValue"]
        for key in ("traceparent",)
        if key in attrs and attrs[key].get("StringValue")
    }
    if trace_context_key is not None:
        signature = attrs.get("traceauth", {}).get("StringValue", "")
        parent = carrier.get("traceparent", "")
        try:
            valid = bool(isinstance(parent, str) and parent and isinstance(signature, str) and
                         re.fullmatch(r"[0-9a-f]{64}", signature) and
                         hmac.compare_digest(
                             hmac.new(trace_context_key, parent.encode("ascii"), hashlib.sha256).hexdigest(),
                             signature))
        except UnicodeEncodeError:
            valid = False
        if not valid:
            carrier = {}
    context = TraceContextTextMapPropagator().extract(carrier)
    with trace.get_tracer("togglemaster.sqs").start_as_current_span(
        "sqs process", context=context, kind=SpanKind.CONSUMER
    ) as span:
        outcome = _process_message(message)
        event = {"event": "sqs.process", "service": "analytics-service", "outcome": outcome}
        span_context = span.get_span_context()
        if span_context.is_valid:
            event["trace_id"] = format(span_context.trace_id, "032x")
            event["span_id"] = format(span_context.span_id, "016x")
        event_log.info(json.dumps(event))


def traced_aws_call(name, call, **kwargs):
    with trace.get_tracer("togglemaster.aws").start_as_current_span(
        name, kind=SpanKind.CLIENT, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            return call(**kwargs)
        except Exception:
            span.set_status(Status(StatusCode.ERROR, "AWS operation failed"))
            raise


def _process_message(message):
    """ Processa uma única mensagem SQS e a insere no DynamoDB """
    try:
        log.info(f"Processando mensagem ID: {message['MessageId']}")
        body = json.loads(message['Body'])

        # Gera um ID único para o item no DynamoDB
        event_id = str(uuid.uuid4())

        # Constrói o item no formato do DynamoDB
        item = {
            'event_id': {'S': event_id},
            'user_id': {'S': body['user_id']},
            'flag_name': {'S': body['flag_name']},
            'result': {'BOOL': body['result']},
            'timestamp': {'S': body['timestamp']}
        }

        # Insere no DynamoDB
        traced_aws_call(
            "dynamodb put_item",
            dynamodb_client.put_item,
            TableName=DYNAMODB_TABLE_NAME,
            Item=item
        )

        log.info(f"Evento {event_id} (Flag: {body['flag_name']}) salvo no DynamoDB.")

        # Se tudo deu certo, deleta a mensagem da fila
        traced_aws_call(
            "sqs delete_message",
            sqs_client.delete_message,
            QueueUrl=SQS_QUEUE_URL,
            ReceiptHandle=message['ReceiptHandle']
        )
        processed_messages.add(1, {"outcome": "success"})
        return "success"

    except json.JSONDecodeError:
        processed_messages.add(1, {"outcome": "error"})
        trace.get_current_span().set_status(Status(StatusCode.ERROR, "invalid message"))
        log.error(f"Erro ao decodificar JSON da mensagem ID: {message['MessageId']}")
        # Não deleta a mensagem, pode ser uma "poison pill"
        return "error"
    except ClientError as e:
        processed_messages.add(1, {"outcome": "error"})
        trace.get_current_span().set_status(Status(StatusCode.ERROR, "AWS processing error"))
        log.error(f"Erro do Boto3 (DynamoDB ou SQS) ao processar {message['MessageId']}: {e}")
        # Não deleta a mensagem, tenta novamente
        return "error"
    except Exception as e:
        processed_messages.add(1, {"outcome": "error"})
        trace.get_current_span().set_status(Status(StatusCode.ERROR, "message processing error"))
        log.error(f"Erro inesperado ao processar {message['MessageId']}: {e}")
        # Não deleta a mensagem, tenta novamente
        return "error"


def sqs_worker_loop():
    """ Loop principal do worker que ouve a fila SQS """
    log.info("Iniciando o worker SQS...")
    while True:
        try:
            # Long-polling: espera até 20s por mensagens
            response = sqs_client.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=10,  # Processa em lotes de até 10
                WaitTimeSeconds=20,
                MessageAttributeNames=["traceparent", "traceauth"],
            )

            messages = response.get('Messages', [])
            if not messages:
                # Nenhuma mensagem, continua o loop
                continue

            log.info(f"Recebidas {len(messages)} mensagens.")

            for message in messages:
                process_message(message)

        except ClientError as e:
            log.error(f"Erro do Boto3 no loop principal do SQS: {e}")
            time.sleep(10)  # Pausa antes de tentar novamente
        except Exception as e:
            log.error(f"Erro inesperado no loop principal do SQS: {e}")
            time.sleep(10)

# --- Servidor Flask (Apenas para Health Check) ---


app = Flask(__name__)


@app.route('/health')
def health():
    # Uma verificação de saúde real poderia checar a conexão com o DynamoDB/SQS
    return jsonify({"status": "ok"})

# --- Inicialização ---


def start_worker():
    """ Inicia N threads consumidoras da fila SQS.

    Cada thread faz seu próprio long-poll/receive; a SQS entrega mensagens
    diferentes para receives concorrentes (visibility timeout evita duplicidade),
    portanto múltiplas threads (e múltiplos pods) não processam a mesma mensagem.
    Mais threads = maior vazão e maior uso de CPU sob carga, o que permite ao
    HPA por CPU detectar a pressão da fila e escalar os pods.
    """
    log.info(f"Iniciando {WORKER_THREADS} thread(s) consumidora(s) da SQS...")
    for i in range(WORKER_THREADS):
        worker_thread = threading.Thread(
            target=sqs_worker_loop, name=f"sqs-worker-{i}", daemon=True
        )
        worker_thread.start()


# Inicia o worker SQS em uma thread de background
# Isso garante que ele inicie tanto com 'flask run' quanto com 'gunicorn'
start_worker()

if __name__ == '__main__':
    port = int(os.getenv("PORT", 8005))
    app.run(host='0.0.0.0', port=port, debug=False)  # nosec B104
