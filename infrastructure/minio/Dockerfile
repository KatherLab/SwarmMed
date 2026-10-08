# MinIO community edition, built from pinned upstream source.
#
# MinIO stopped publishing community-edition images: minio/minio no longer exists
# on Docker Hub and quay.io/minio/minio is gone as well, so `docker compose up`
# could not start a fresh deployment. The stack therefore builds the exact release
# it was developed and validated against, unmodified, from the upstream repository
# (GNU AGPLv3, https://github.com/minio/minio).
ARG MINIO_RELEASE=RELEASE.2025-09-07T16-13-09Z

FROM golang:1.24-bookworm AS build
ARG MINIO_RELEASE
RUN git clone --depth 1 --branch "${MINIO_RELEASE}" https://github.com/minio/minio.git /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -tags kqueue -trimpath \
      -ldflags "$(go run buildscripts/gen-ldflags.go)" -o /out/minio . \
 && /out/minio --version

FROM debian:bookworm-slim
ARG MINIO_RELEASE
LABEL org.opencontainers.image.title="minio" \
      org.opencontainers.image.version="${MINIO_RELEASE}" \
      org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/minio /usr/bin/minio
COPY docker-entrypoint.sh /usr/bin/docker-entrypoint.sh
RUN chmod +x /usr/bin/docker-entrypoint.sh
EXPOSE 9000 9001
ENTRYPOINT ["/usr/bin/docker-entrypoint.sh"]
CMD ["minio"]
