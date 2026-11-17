package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/go-redis/redis/v8"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

type unavailableRedis struct{}

func (unavailableRedis) BeforeProcess(ctx context.Context, _ redis.Cmder) (context.Context, error) {
	return ctx, errors.New("synthetic Redis failure")
}
func (unavailableRedis) AfterProcess(context.Context, redis.Cmder) error { return nil }
func (unavailableRedis) BeforeProcessPipeline(ctx context.Context, _ []redis.Cmder) (context.Context, error) {
	return ctx, errors.New("synthetic Redis failure")
}
func (unavailableRedis) AfterProcessPipeline(context.Context, []redis.Cmder) error { return nil }

func TestCacheFailureKeepsFallbackAndCreatesRedisSpans(t *testing.T) {
	old := otel.GetTracerProvider()
	exporter := tracetest.NewInMemoryExporter()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter)))
	defer otel.SetTracerProvider(old)
	client := redis.NewClient(&redis.Options{Addr: "127.0.0.1:1", MaxRetries: 0})
	defer client.Close()
	client.AddHook(unavailableRedis{})
	app := &App{
		RedisClient:         client,
		FlagServiceURL:      "http://flag",
		TargetingServiceURL: "http://targeting",
		HttpClient: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			body := `{"name":"demo","is_enabled":true}`
			if r.URL.Host == "targeting" {
				body = `{"flag_name":"demo","is_enabled":true,"rules":{"type":"PERCENTAGE","value":100}}`
			}
			return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
		})},
	}
	ctx, parent := otel.Tracer("test").Start(context.Background(), "parent")
	info, err := app.getCombinedFlagInfo(ctx, "demo")
	parent.End()
	if err != nil || info.Flag == nil || !info.Flag.IsEnabled {
		t.Fatalf("fallback falhou: %v", err)
	}
	var get, set bool
	for _, span := range exporter.GetSpans() {
		if span.Name == "redis get" || span.Name == "redis set" {
			if span.Parent.SpanID() != parent.SpanContext().SpanID() {
				t.Fatalf("span Redis sem o parent esperado: %s", span.Name)
			}
			get = get || span.Name == "redis get"
			set = set || span.Name == "redis set"
		}
	}
	if !get || !set {
		t.Fatal("spans de Redis ausentes")
	}
}
