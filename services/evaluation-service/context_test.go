package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
)

type contextKey struct{}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestFetchFromServicesPropagatesConcurrentContexts(t *testing.T) {
	var mu sync.Mutex
	seen := map[string]int{}
	app := &App{
		FlagServiceURL:      "http://flag",
		TargetingServiceURL: "http://targeting",
		HttpClient: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			value, ok := r.Context().Value(contextKey{}).(string)
			if !ok {
				t.Error("contexto ausente na requisição")
			}
			mu.Lock()
			seen[value]++
			mu.Unlock()
			body := `{"name":"demo","is_enabled":true}`
			if r.URL.Host == "targeting" {
				body = `{"flag_name":"demo","is_enabled":true,"rules":{"type":"PERCENTAGE","value":100}}`
			}
			return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
		})},
	}

	var wg sync.WaitGroup
	for _, value := range []string{"primeiro", "segundo"} {
		wg.Add(1)
		go func(value string) {
			defer wg.Done()
			ctx := context.WithValue(context.Background(), contextKey{}, value)
			if _, err := app.fetchFromServices(ctx, "demo"); err != nil {
				t.Errorf("fetchFromServices: %v", err)
			}
		}(value)
	}
	wg.Wait()
	if seen["primeiro"] != 2 || seen["segundo"] != 2 {
		t.Errorf("contexts crossed or missing: %v", seen)
	}
}

func TestFetchFlagHonorsCancellation(t *testing.T) {
	app := &App{
		FlagServiceURL: "http://flag",
		HttpClient: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return nil, r.Context().Err()
		})},
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := app.fetchFlag(ctx, "demo"); !errors.Is(err, context.Canceled) {
		t.Fatalf("erro = %v; esperado context.Canceled", err)
	}
}
