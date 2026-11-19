package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
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
	req.Header.Set("tracestate", "demo=private-canary")
	resp := httptest.NewRecorder()
	handler.ServeHTTP(resp, req)
	if resp.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d", resp.Code)
	}

	spans := exporter.GetSpans()
	if len(spans) != 1 || spans[0].Parent.TraceID().String() != "0123456789abcdef0123456789abcdef" || !spans[0].Parent.IsRemote() {
		t.Fatalf("span ou parent inesperado: %+v", spans)
	}
	if spans[0].SpanContext.TraceState().String() != "" || spans[0].Parent.TraceState().String() != "" {
		t.Fatal("tracestate não confiável foi preservado")
	}
	for _, attr := range spans[0].Attributes {
		if strings.Contains(attr.Value.Emit(), "private-user") {
			t.Fatalf("atributo contém dado de requisição: %s", attr.Key)
		}
	}

}

func TestTraceContextRequiresValidSignature(t *testing.T) {
	oldKey, oldProvider, oldPropagator := traceContextKey, otel.GetTracerProvider(), otel.GetTextMapPropagator()
	traceContextKey = []byte(strings.Repeat("x", 32))
	exporter := tracetest.NewInMemoryExporter()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer func() {
		traceContextKey = oldKey
		otel.SetTracerProvider(oldProvider)
		otel.SetTextMapPropagator(oldPropagator)
	}()
	parent := "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
	mac := hmac.New(sha256.New, traceContextKey)
	_, _ = mac.Write([]byte(parent))
	signature := hex.EncodeToString(mac.Sum(nil))
	handler := traceHandler("/validate", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	for _, signature := range []string{"", strings.Repeat("0", 64), signature} {
		req := httptest.NewRequest(http.MethodGet, "/validate", nil)
		req.Header.Set("traceparent", parent)
		req.Header.Set("X-Trace-Auth", signature)
		handler.ServeHTTP(httptest.NewRecorder(), req)
	}
	spans := exporter.GetSpans()
	if len(spans) != 3 || spans[0].Parent.IsValid() || spans[1].Parent.IsValid() ||
		spans[2].Parent.TraceID().String() != "0123456789abcdef0123456789abcdef" {
		t.Fatal("contexto não autenticado aceito ou contexto assinado rejeitado")
	}
}
