# ---- Build stage ----
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS builder

ARG TARGETOS
ARG TARGETARCH

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -mod=readonly -ldflags="-s -w" -o /evaluation-service .

# ---- Runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot

WORKDIR /
COPY --from=builder /evaluation-service /evaluation-service

EXPOSE 8004
USER nonroot:nonroot
ENTRYPOINT ["/evaluation-service"]
