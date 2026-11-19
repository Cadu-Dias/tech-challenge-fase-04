package main

import (
	"context"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var eventLogger = slog.New(slog.NewJSONHandler(os.Stdout, nil))

func initTelemetry(service string) (func(), error) {
	if err := loadTraceContextKey(); err != nil {
		return nil, err
	}
	otel.SetTextMapPropagator(propagation.TraceContext{})
	res := resource.NewWithAttributes("", attribute.String("service.name", service))
	var traces *sdktrace.TracerProvider
	var metrics *sdkmetric.MeterProvider
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") != "" {
		exporter, err := otlptracehttp.New(context.Background(),
			otlptracehttp.WithTimeout(2*time.Second),
			otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: false}))
		if err != nil {
			return nil, fmt.Errorf("configurar exportador de traces: %w", err)
		}
		traces = sdktrace.NewTracerProvider(sdktrace.WithResource(res),
			sdktrace.WithBatcher(exporter, sdktrace.WithMaxQueueSize(512),
				sdktrace.WithBatchTimeout(time.Second), sdktrace.WithExportTimeout(2*time.Second)))
		otel.SetTracerProvider(traces)
	} else {
		log.Print("OpenTelemetry: exportação de traces desabilitada")
	}
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") != "" {
		exporter, err := otlpmetrichttp.New(context.Background(),
			otlpmetrichttp.WithTimeout(2*time.Second),
			otlpmetrichttp.WithRetry(otlpmetrichttp.RetryConfig{Enabled: false}))
		if err != nil {
			if traces != nil {
				_ = traces.Shutdown(context.Background())
			}
			return nil, fmt.Errorf("configurar exportador de métricas: %w", err)
		}
		metrics = sdkmetric.NewMeterProvider(sdkmetric.WithResource(res),
			sdkmetric.WithView(sdkmetric.NewView(
				sdkmetric.Instrument{Name: "http.server.request.duration"},
				sdkmetric.Stream{Aggregation: sdkmetric.AggregationExplicitBucketHistogram{
					Boundaries: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
				}},
			)),
			sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exporter,
				sdkmetric.WithInterval(10*time.Second), sdkmetric.WithTimeout(2*time.Second))))
		otel.SetMeterProvider(metrics)
	} else {
		log.Print("OpenTelemetry: exportação de métricas desabilitada")
	}
	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if metrics != nil {
			if err := metrics.Shutdown(ctx); err != nil {
				log.Printf("Falha ao encerrar exportação de métricas: %v", err)
			}
		}
		if traces != nil {
			if err := traces.Shutdown(ctx); err != nil {
				log.Printf("Falha ao encerrar exportação de traces: %v", err)
			}
		}
	}, nil
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(status int) {
	if w.status == 0 {
		w.status = status
		w.ResponseWriter.WriteHeader(status)
	}
}

func (w *statusWriter) Write(b []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(b)
}

func traceHandler(route string, handler http.Handler) http.Handler {
	duration, err := otel.Meter("togglemaster.http").Float64Histogram("http.server.request.duration", metric.WithUnit("s"))
	if err != nil {
		log.Printf("Falha ao criar métrica HTTP: %v", err)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		method := normalizedMethod(r.Method)
		carrier := propagation.HeaderCarrier(http.Header{})
		parents, signatures := r.Header.Values("Traceparent"), r.Header.Values("X-Trace-Auth")
		if len(parents) == 1 && (traceContextKey == nil ||
			(len(signatures) == 1 && trustedTraceparent(parents[0], signatures[0]))) {
			carrier.Set("traceparent", parents[0])
		}
		ctx := otel.GetTextMapPropagator().Extract(r.Context(), carrier)
		ctx, span := otel.Tracer("togglemaster/http").Start(ctx, route, trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(attribute.String("http.request.method", method), attribute.String("http.route", route)))
		defer span.End()
		writer := &statusWriter{ResponseWriter: w}
		handler.ServeHTTP(writer, r.WithContext(ctx))
		status := writer.status
		if status == 0 {
			status = http.StatusOK
		}
		span.SetAttributes(attribute.Int("http.response.status_code", status))
		if err == nil {
			duration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(
				attribute.String("http.request.method", method),
				attribute.String("http.route", route),
				attribute.Int("http.response.status_code", status)))
		}
		if status >= 500 {
			span.SetStatus(codes.Error, "HTTP server error")
		}
		attrs := []any{"event", "http.request", "route", route, "method", method, "status", status}
		if sc := span.SpanContext(); sc.IsValid() {
			attrs = append(attrs, "trace_id", sc.TraceID().String(), "span_id", sc.SpanID().String())
		}
		eventLogger.Info("request completed", attrs...)
	})
}

func normalizedMethod(method string) string {
	switch method {
	case http.MethodGet, http.MethodHead, http.MethodPost, http.MethodPut,
		http.MethodPatch, http.MethodDelete, http.MethodOptions:
		return method
	default:
		return "_OTHER"
	}
}
