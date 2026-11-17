package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

func TestHTTPMetricUsesNormalizedRouteAndStatus(t *testing.T) {
	old := otel.GetMeterProvider()
	reader := sdkmetric.NewManualReader()
	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	otel.SetMeterProvider(provider)
	defer otel.SetMeterProvider(old)
	handler := traceHandler("/evaluate", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/evaluate?user_id=private-user", nil))

	var data metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &data); err != nil {
		t.Fatal(err)
	}

	if len(data.ScopeMetrics) != 1 || len(data.ScopeMetrics[0].Metrics) != 1 {
		t.Fatalf("métricas HTTP ausentes: %+v", data)
	}
	m := data.ScopeMetrics[0].Metrics[0]
	hist, ok := m.Data.(metricdata.Histogram[float64])
	if m.Name != "http.server.request.duration" || !ok || len(hist.DataPoints) != 1 || hist.DataPoints[0].Count != 1 {
		t.Fatalf("métrica HTTP inesperada: %+v", m)
	}
	attrs := hist.DataPoints[0].Attributes.ToSlice()
	foundRoute, foundStatus := false, false
	for _, attr := range attrs {
		if strings.Contains(attr.Value.Emit(), "private-user") {
			t.Fatal("métrica contém dados da query")
		}
		foundRoute = foundRoute || string(attr.Key) == "http.route" && attr.Value.AsString() == "/evaluate"
		foundStatus = foundStatus || string(attr.Key) == "http.response.status_code" && attr.Value.AsInt64() == 502
	}
	if !foundRoute || !foundStatus {
		t.Fatalf("atributos esperados ausentes: %+v", attrs)
	}
}

func TestUnknownHTTPMethodIsNormalized(t *testing.T) {
	if got := normalizedMethod("USER-SUPPLIED"); got != "_OTHER" {
		t.Fatalf("método inesperado: %s", got)
	}
}
