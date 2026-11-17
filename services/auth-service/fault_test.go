package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestControlledFaultIsOptInAndReversible(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "marker")
	if err := injectFault(false, marker); err == nil {
		t.Fatal("injeção deveria estar desabilitada")
	}
	if err := injectFault(true, marker); err != nil {
		t.Fatal(err)
	}
	if err := injectFault(true, marker); err != nil {
		t.Fatalf("injeção repetida deve ser idempotente: %v", err)
	}

	app := &App{FaultMarker: marker}
	req := httptest.NewRequest(http.MethodGet, "/validate", nil)
	response := httptest.NewRecorder()
	app.validateKeyHandler(response, req)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("falha desabilitada alterou comportamento: %d", response.Code)
	}
	app.FaultEnabled = true
	response = httptest.NewRecorder()
	app.validateKeyHandler(response, req)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("falha ativada não retornou 503: %d", response.Code)
	}
	health := httptest.NewRecorder()
	app.healthHandler(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusOK {
		t.Fatalf("falha afetou health: %d", health.Code)
	}
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	response = httptest.NewRecorder()
	app.validateKeyHandler(response, req)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("falha não foi revertida: %d", response.Code)
	}
}
