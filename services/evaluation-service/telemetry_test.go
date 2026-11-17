package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestTraceHandlerAndClientPropagateWithoutRequestData(t *testing.T) {
	oldProvider, oldPropagator := otel.GetTracerProvider(), otel.GetTextMapPropagator()
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer func() {
		otel.SetTracerProvider(oldProvider)
		otel.SetTextMapPropagator(oldPropagator)
	}()

	var outbound string
	app := &App{HttpClient: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		outbound = r.Header.Get("traceparent")
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("{}")), Header: make(http.Header)}, nil
	})}}
	handler := traceHandler("/evaluate", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, "http://flag/private-name", nil)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := app.doTracedRequest(req, "flag-service")
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		w.WriteHeader(http.StatusBadGateway)
	}))
	req := httptest.NewRequest(http.MethodGet, "/evaluate?user_id=private-user", nil)
	req.Header.Set("traceparent", "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
	req.Header.Set("Authorization", "Bearer private-secret")
	handler.ServeHTTP(httptest.NewRecorder(), req)

	if !strings.HasPrefix(outbound, "00-0123456789abcdef0123456789abcdef-") {
		t.Fatalf("traceparent não propagado: %q", outbound)
	}
	spans := exporter.GetSpans()
	if len(spans) != 2 || spans[0].Parent.SpanID() != spans[1].SpanContext.SpanID() {
		t.Fatalf("hierarquia de spans inesperada: %+v", spans)
	}
	for _, span := range spans {
		for _, attr := range span.Attributes {
			value := attr.Value.Emit()
			if strings.Contains(value, "private-") {
				t.Fatalf("atributo contém dado de requisição: %s", attr.Key)
			}
		}
	}
}
