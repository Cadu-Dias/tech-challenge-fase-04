import atexit
import json
import logging
import os
import time

from flask import g, request
import requests
from opentelemetry import context, metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

log = logging.getLogger(__name__)
event_log = logging.getLogger("togglemaster.events")
event_log.setLevel(logging.INFO)
event_log.propagate = False
if not event_log.handlers:
    event_log.addHandler(logging.StreamHandler())
propagator = TraceContextTextMapPropagator()


def instrument(app, service):
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource({"service.name": service}))
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
        from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
        from opentelemetry.sdk.resources import Resource

        reader = PeriodicExportingMetricReader(OTLPMetricExporter(timeout=2), export_interval_millis=10000)
        meter_provider = MeterProvider(
            resource=Resource({"service.name": service}), metric_readers=[reader],
            views=[View(
                instrument_name="http.server.request.duration",
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
                ),
            )],
        )
        metrics.set_meter_provider(meter_provider)
        atexit.register(meter_provider.shutdown)
    else:
        log.info("OpenTelemetry: endpoint não configurado; exportação de métricas desabilitada")

    duration = metrics.get_meter("togglemaster.http").create_histogram(
        "http.server.request.duration", unit="s"
    )

    @app.before_request
    def start_request_span():
        if request.path == "/health":
            return
        parent = propagator.extract(request.headers)
        route = request.url_rule.rule if request.url_rule else "unmatched"
        method = request.method if request.method in {
            "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"
        } else "_OTHER"
        g.otel_start = time.monotonic()
        g.otel_route = route
        g.otel_method = method
        span = trace.get_tracer("togglemaster.http").start_span(
            route, context=parent, kind=SpanKind.SERVER,
            attributes={"http.route": route, "http.request.method": method},
        )
        g.otel_span = span
        g.otel_token = context.attach(trace.set_span_in_context(span, parent))

    @app.after_request
    def finish_response(response):
        span = getattr(g, "otel_span", None)
        if span is not None:
            span.set_attribute("http.response.status_code", response.status_code)
            duration.record(time.monotonic() - g.otel_start, {
                "http.route": g.otel_route,
                "http.request.method": g.otel_method,
                "http.response.status_code": response.status_code,
            })
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR, "HTTP server error"))
            event = {
                "event": "http.request", "service": service,
                "route": g.otel_route, "method": g.otel_method, "status": response.status_code,
            }
            span_context = span.get_span_context()
            if span_context.is_valid:
                event["trace_id"] = format(span_context.trace_id, "032x")
                event["span_id"] = format(span_context.span_id, "016x")
            event_log.info(json.dumps(event))
        return response

    @app.teardown_request
    def finish_request(error):
        span = getattr(g, "otel_span", None)
        token = getattr(g, "otel_token", None)
        if span is not None:
            if error is not None:
                span.set_status(Status(StatusCode.ERROR, "HTTP server error"))
            span.end()
            g.otel_span = None
        if token is not None:
            context.detach(token)
            g.otel_token = None


def auth_get(url, auth_header):
    with trace.get_tracer("togglemaster.http").start_as_current_span(
        "GET auth-service", kind=SpanKind.CLIENT,
        record_exception=False, set_status_on_exception=False,
    ) as span:
        headers = {"Authorization": auth_header}
        propagator.inject(headers)
        try:
            response = requests.get(url, headers=headers, timeout=3)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR, "HTTP server error"))
            return response
        except requests.exceptions.RequestException:
            span.set_status(Status(StatusCode.ERROR, "HTTP client error"))
            raise
