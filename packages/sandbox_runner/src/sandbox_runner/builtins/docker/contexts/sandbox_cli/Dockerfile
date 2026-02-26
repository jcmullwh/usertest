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

# Cache wiring:
# - sandbox_runner mounts a host cache dir at /cache when cache_mode=warm.
# - keep agent login/config under /root (runner mounts those explicitly).
# - but redirect heavyweight tool caches (pip + PDM) into /cache so subsequent runs are faster.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDM_CHECK_UPDATE=false

RUN mkdir -p /cache/pip /cache/pdm /cache/pdm-share \
    && mkdir -p /root/.cache /root/.local/share \
    && rm -rf /root/.cache/pip /root/.cache/pdm /root/.local/share/pdm \
    && ln -s /cache/pip /root/.cache/pip \
    && ln -s /cache/pdm /root/.cache/pdm \
    && ln -s /cache/pdm-share /root/.local/share/pdm

WORKDIR /workspace
