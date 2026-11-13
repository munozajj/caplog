# syntax=docker/dockerfile:1
#
# CapLog container image.
# Stage 1 compiles a static Go binary; stage 2 copies just that binary into a
# "distroless" image: no shell, no package manager, runs as a non-root user.

# ---- build stage ----
# --platform=$BUILDPLATFORM: compile natively and cross-compile for the target
# (fast, no emulation needed for arm64 builds).
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS build
ARG TARGETOS
ARG TARGETARCH
ARG VERSION=dev
WORKDIR /src

# Download dependencies first so this layer is cached between builds.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
# CGO_ENABLED=0 -> fully static binary (the SQLite driver is pure Go).
# -s -w strips debug info to keep the image small.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/caplog . \
 && mkdir -p /out/data /out/checkpoints

# ---- runtime stage ----
FROM gcr.io/distroless/static-debian13:nonroot
COPY --from=build /out/caplog /caplog
# Empty mount points owned by the nonroot user (uid 65532), so new Docker
# volumes start out writable by the app.
COPY --from=build --chown=65532:65532 /out/data /data
COPY --from=build --chown=65532:65532 /out/checkpoints /checkpoints

ENV CAPLOG_LISTEN=:8080 \
    CAPLOG_DB=/data/caplog.db \
    CAPLOG_CHECKPOINTS=/checkpoints/checkpoints.jsonl
USER 65532:65532
EXPOSE 8080
VOLUME ["/data", "/checkpoints"]
# No curl/wget in the image, so the binary checks itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["/caplog", "healthcheck"]
ENTRYPOINT ["/caplog"]
CMD ["serve"]
