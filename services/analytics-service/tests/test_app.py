"""Testes de unidade para o analytics-service (health check e worker SQS)."""
import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_process_message_success_deletes_from_queue(app_module):
    message = {
        "MessageId": "msg-1",
        "ReceiptHandle": "receipt-1",
        "Body": json.dumps(
            {
                "user_id": "user-1",
                "flag_name": "demo",
                "result": True,
                "timestamp": "2024-01-01T00:00:00Z",
            }
        ),
    }

    app_module.process_message(message)

    assert app_module.dynamodb_client.put_calls, "esperava um put_item no DynamoDB"
    table_name, item = app_module.dynamodb_client.put_calls[0]
    assert table_name == "ToggleMasterAnalytics"
    assert item["flag_name"]["S"] == "demo"
    assert item["result"]["BOOL"] is True
    assert "receipt-1" in app_module.sqs_client.deleted


def test_process_message_invalid_json_does_not_delete(app_module):
    message = {
        "MessageId": "msg-2",
        "ReceiptHandle": "receipt-2",
        "Body": "{invalid-json",
    }

    app_module.process_message(message)

    assert not app_module.dynamodb_client.put_calls
    assert "receipt-2" not in app_module.sqs_client.deleted


def test_process_message_missing_field_does_not_delete(app_module):
    message = {
        "MessageId": "msg-3",
        "ReceiptHandle": "receipt-3",
        "Body": json.dumps({"user_id": "user-1"}),  # falta flag_name/result/timestamp
    }

    app_module.process_message(message)

    assert not app_module.dynamodb_client.put_calls
    assert "receipt-3" not in app_module.sqs_client.deleted


def test_process_message_extracts_and_isolates_trace_context(app_module, monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(app_module.trace, "get_tracer", lambda _: tracer)
    events = []
    monkeypatch.setattr(app_module.event_log, "info", events.append)
    message = {
        "MessageId": "msg-trace",
        "ReceiptHandle": "receipt-trace",
        "Body": json.dumps({
            "user_id": "synthetic",
            "flag_name": "demo",
            "result": True,
            "timestamp": "2024-01-01T00:00:00Z",
        }),
        "MessageAttributes": {
            "traceparent": {"StringValue": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
        },
    }

    app_module.process_message(message)
    message.pop("MessageAttributes")
    app_module.process_message(message)

    spans = exporter.get_finished_spans()
    first, second = [span for span in spans if span.name == "sqs process"]
    assert first.context.trace_id == int("0123456789abcdef0123456789abcdef", 16)
    assert first.parent.span_id == int("0123456789abcdef", 16)
    assert first.parent.is_remote
    assert second.parent is None
    assert second.context.trace_id != first.context.trace_id
    first_children = [span for span in spans if span.parent and span.parent.span_id == first.context.span_id]
    assert {span.name for span in first_children} == {"dynamodb put_item", "sqs delete_message"}
    event = json.loads(events[0])
    assert event["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert event["outcome"] == "success"
    assert "synthetic" not in json.dumps(event)
    provider.shutdown()


def test_process_message_records_only_outcome(app_module, monkeypatch):
    observed = []

    class Counter:
        def add(self, amount, attributes):
            observed.append((amount, attributes))

    monkeypatch.setattr(app_module, "processed_messages", Counter())
    message = {
        "MessageId": "msg",
        "ReceiptHandle": "receipt",
        "Body": json.dumps({
            "user_id": "synthetic",
            "flag_name": "demo",
            "result": True,
            "timestamp": "2024-01-01T00:00:00Z",
        }),
    }
    app_module.process_message(message)
    message["Body"] = "{invalid-json"
    app_module.process_message(message)
    assert observed == [(1, {"outcome": "success"}), (1, {"outcome": "error"})]
