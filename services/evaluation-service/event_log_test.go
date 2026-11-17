package main

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func TestRequestEventCorrelatesWithoutRequestData(t *testing.T) {
	var output bytes.Buffer
	oldLogger, oldProvider, oldPropagator := eventLogger, otel.GetTracerProvider(), otel.GetTextMapPropagator()
	provider := sdktrace.NewTracerProvider()
	eventLogger = slog.New(slog.NewJSONHandler(&output, nil))
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer func() {
		eventLogger = oldLogger
		otel.SetTracerProvider(oldProvider)
		otel.SetTextMapPropagator(oldPropagator)
	}()
	req := httptest.NewRequest(http.MethodGet, "/evaluate?user_id=private-user", nil)
	req.Header.Set("Authorization", "Bearer private-secret")
	req.Header.Set("traceparent", "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
	traceHandler("/evaluate", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	})).ServeHTTP(httptest.NewRecorder(), req)
	var event map[string]any
	if err := json.Unmarshal(output.Bytes(), &event); err != nil {
		t.Fatal(err)
	}
	if event["trace_id"] != "0123456789abcdef0123456789abcdef" || event["route"] != "/evaluate" || event["status"] != float64(502) {
		t.Fatalf("evento inesperado: %v", event)
	}
	if strings.Contains(output.String(), "private-") {
		t.Fatal("evento contém dados da requisição")
	}
}
