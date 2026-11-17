# ---- Build stage ----
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS builder

ARG TARGETOS
ARG TARGETARCH

WORKDIR /app

# Cache de dependências
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -mod=readonly -ldflags="-s -w" -o /auth-service .

# ---- Runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /
COPY --from=builder /auth-service /auth-service
COPY --from=builder /app/db /db

EXPOSE 8001
USER nonroot:nonroot
ENTRYPOINT ["/auth-service"]
