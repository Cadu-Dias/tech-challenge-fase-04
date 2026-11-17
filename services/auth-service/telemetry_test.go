package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestValidateTraceContinuesParentWithoutRequestData(t *testing.T) {
	oldProvider, oldPropagator := otel.GetTracerProvider(), otel.GetTextMapPropagator()
	exporter := tracetest.NewInMemoryExporter()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer func() {
		otel.SetTracerProvider(oldProvider)
		otel.SetTextMapPropagator(oldPropagator)
	}()
	app := &App{}
	handler := traceHandler("/validate", http.HandlerFunc(app.validateKeyHandler))
	req := httptest.NewRequest(http.MethodGet, "/validate?private-user=test", nil)
	req.Header.Set("traceparent", "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
	resp := httptest.NewRecorder()
	handler.ServeHTTP(resp, req)
	if resp.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d", resp.Code)
	}
	spans := exporter.GetSpans()
	if len(spans) != 1 || spans[0].Parent.TraceID().String() != "0123456789abcdef0123456789abcdef" || !spans[0].Parent.IsRemote() {
		t.Fatalf("span ou parent inesperado: %+v", spans)
	}
	for _, attr := range spans[0].Attributes {
		if strings.Contains(attr.Value.Emit(), "private-user") {
			t.Fatalf("atributo contém dado de requisição: %s", attr.Key)
		}
	}
}
