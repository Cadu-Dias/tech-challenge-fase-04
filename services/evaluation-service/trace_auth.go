package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
)

var traceContextKey []byte

func loadTraceContextKey() error {
	path := os.Getenv("TRACE_CONTEXT_HMAC_KEY_FILE")
	traceContextKey = nil
	if path == "" {
		return nil
	}
	key, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("ler chave de contexto de trace: %w", err)
	}
	key = bytes.TrimSpace(key)
	if len(key) < 32 || len(key) > 1024 {
		return fmt.Errorf("chave de contexto de trace deve ter entre 32 e 1024 bytes")
	}
	traceContextKey = key
	return nil
}

func traceSignature(parent string) string {
	if traceContextKey == nil || parent == "" {
		return ""
	}
	mac := hmac.New(sha256.New, traceContextKey)
	_, _ = mac.Write([]byte(parent))
	return hex.EncodeToString(mac.Sum(nil))
}
