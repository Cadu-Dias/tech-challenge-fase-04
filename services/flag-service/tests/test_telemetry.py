import json

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import telemetry
from flask import Flask


def test_http_trace_propagates_without_request_data(app_module, monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda _: tracer)
    outgoing = {}
    events = []
    monkeypatch.setattr(telemetry.event_log, "info", events.append)

    def fake_get(url, headers, timeout):
        outgoing.update(headers)

        class Response:
            status_code = 200

        return Response()

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    client = app_module.app.test_client()
    response = client.get(
        "/flags/private-name?user_id=private-user",
        headers={
            "Authorization": "Bearer private-secret",
            "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        },
    )
    assert response.status_code == 200
    spans = exporter.get_finished_spans()
    client_span, server_span = spans
    assert server_span.parent.is_remote
    assert client_span.parent.span_id == server_span.context.span_id
    assert outgoing["traceparent"].startswith(
        "00-0123456789abcdef0123456789abcdef-" + format(client_span.context.span_id, "016x")
    )
    assert all(
        "private-" not in str(value)
        for span in spans
        for value in span.attributes.values()
    )
    event = json.loads(events[-1])
    assert event["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert event["route"] == "/flags/<string:name>"
    assert "private-" not in json.dumps(event)
    provider.shutdown()


def test_http_metrics_use_normalized_route(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(resource=Resource({}), metric_readers=[reader])
    monkeypatch.setattr(telemetry.metrics, "get_meter", lambda _: provider.get_meter("test"))
    app = Flask(__name__)
    telemetry.instrument(app, "test")

    @app.get("/flags/<name>")
    def flag(name):
        return "", 503

    response = app.test_client().get("/flags/private-name?user_id=private-user")
    assert response.status_code == 503
    data = reader.get_metrics_data()
    metric = data.resource_metrics[0].scope_metrics[0].metrics[0]
    point = metric.data.data_points[0]
    assert metric.name == "http.server.request.duration"
    assert point.count == 1
    assert point.attributes["http.route"] == "/flags/<name>"
    assert point.attributes["http.response.status_code"] == 503
    assert "private-" not in str(point.attributes)
    app.test_client().open("/flags/private-name", method="PRIVATE-METHOD")
    points = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
    assert any(point.attributes["http.request.method"] == "_OTHER" for point in points)
    provider.shutdown()
