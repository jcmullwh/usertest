FROM python:3.11-slim

ARG DEBIAN_FRONTEND=noninteractive

WORKDIR /tmp/sandbox_cli_build

COPY manifests/ ./manifests/
COPY overlays/ ./overlays/
COPY scripts/ ./scripts/

RUN chmod +x ./scripts/install_manifests.sh \
    && ./scripts/install_manifests.sh ./manifests \
    && if [ -d ./overlays/manifests ]; then ./scripts/install_manifests.sh ./overlays/manifests; fi \
    && rm -rf /var/lib/apt/lists/* /tmp/sandbox_cli_build

WORKDIR /workspace
