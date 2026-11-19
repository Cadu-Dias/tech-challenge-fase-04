package main

import (
	"context"
	"crypto/md5" // SQS protocol requires the MD5 checksum of the message body.
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go/aws"
	"github.com/aws/aws-sdk-go/aws/credentials"
	"github.com/aws/aws-sdk-go/aws/session"
	"github.com/aws/aws-sdk-go/service/sqs"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func TestSQSPropagatesTraceContext(t *testing.T) {
	oldKey := traceContextKey
	traceContextKey = []byte(strings.Repeat("x", 32))
	defer func() { traceContextKey = oldKey }()
	oldProvider, oldPropagator := otel.GetTracerProvider(), otel.GetTextMapPropagator()
	exporter := tracetest.NewInMemoryExporter()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	defer func() {
		otel.SetTracerProvider(oldProvider)
		otel.SetTextMapPropagator(oldPropagator)
	}()
	var traceparent, signature string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var input struct {
			MessageBody       string `json:"MessageBody"`
			MessageAttributes map[string]struct {
				StringValue string `json:"StringValue"`
			} `json:"MessageAttributes"`
		}

		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			t.Error(err)
		}
		traceparent = input.MessageAttributes["traceparent"].StringValue
		signature = input.MessageAttributes["traceauth"].StringValue
		sum := md5.Sum([]byte(input.MessageBody)) // #nosec G401 -- SQS protocol checksum.
		w.Header().Set("Content-Type", "application/x-amz-json-1.0")
		fmt.Fprintf(w, `{"MessageId":"test","MD5OfMessageBody":"%s"}`, hex.EncodeToString(sum[:]))
	}))
	defer server.Close()
	sess := session.Must(session.NewSession(&aws.Config{
		Region:      aws.String("us-east-1"),
		Endpoint:    aws.String(server.URL),
		Credentials: credentials.NewStaticCredentials("test", "test", ""),
		MaxRetries:  aws.Int(0),
	}))
	app := &App{SqsSvc: sqs.New(sess), SqsQueueURL: server.URL + "/queue"}
	ctx, parent := otel.Tracer("test").Start(context.Background(), "parent")
	app.sendEvaluationEvent(ctx, "synthetic-user", "demo", true)
	parent.End()
	spans := exporter.GetSpans()
	if len(spans) != 2 || spans[0].Parent.SpanID() != spans[1].SpanContext.SpanID() {
		t.Fatalf("hierarquia SQS inesperada: %+v", spans)
	}
	if spans[0].Status.Code == codes.Error {
		t.Fatal("envio SQS ao mock falhou")
	}
	if !strings.HasPrefix(traceparent, "00-"+spans[0].SpanContext.TraceID().String()+"-"+spans[0].SpanContext.SpanID().String()+"-") {
		t.Fatalf("traceparent SQS inesperado: %q", traceparent)
	}
	if signature != traceSignature(traceparent) || signature == "" {
		t.Fatal("assinatura do trace SQS ausente ou incorreta")
	}
}

func TestValidSQSEndpoint(t *testing.T) {
	for _, tt := range []struct {
		url  string
		want bool
	}{
		{"http://localstack:4566", true},
		{"https://example.test", true},
		{"http://user:password@example.test", false},
		{"http://example.test?token=x", false},
		{"ftp://example.test", false},
		{"http://:4566", false},
	} {
		if got := validSQSEndpoint(tt.url); got != tt.want {
			t.Errorf("validSQSEndpoint(%q) = %v; esperado %v", tt.url, got, tt.want)
		}
	}
}
