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

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDM_CHECK_UPDATE=false

RUN mkdir -p /cache/pip /cache/pdm /cache/pdm-share /opt/usertest_maint_seed \
    && mkdir -p /root/.cache /root/.local/share \
    && rm -rf /root/.cache/pip /root/.cache/pdm /root/.local/share/pdm \
    && ln -s /cache/pip /root/.cache/pip \
    && ln -s /cache/pdm /root/.cache/pdm \
    && ln -s /cache/pdm-share /root/.local/share/pdm

WORKDIR /workspace

COPY repo_snapshot/ /workspace/

RUN python -m pip install -r requirements-dev.txt \
    && python -m pip install "$(grep '^pdm==' manifests/pip.txt | tail -n 1)" \
    && python tools/scaffold/scaffold.py run install --all --skip-missing \
    && python tools/scaffold/install_cache_fingerprint.py --repo-root /workspace --all --output /tmp/usertest_maint_seed_manifest.json \
    && python - <<'PY'
import json
import shutil
from pathlib import Path

from tools.scaffold.install_cache_common import safe_cache_project_id

workspace = Path("/workspace")
seed_root = Path("/opt/usertest_maint_seed")
payload = json.loads(Path("/tmp/usertest_maint_seed_manifest.json").read_text(encoding="utf-8"))
seed_manifest = {"schema_version": 1, "projects": []}
for project in payload.get("projects", []):
    project_id = str(project["id"])
    project_path = str(project["path"])
    fingerprint = str(project["fingerprint"])
    source = workspace / project_path / ".venv"
    if not source.exists():
        raise SystemExit(f"Missing seeded venv for {project_id}: {source}")
    dest = seed_root / safe_cache_project_id(project_id) / fingerprint / "venv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    seed_manifest["projects"].append(
        {
            "id": project_id,
            "path": project_path,
            "fingerprint": fingerprint,
            "seed_path": str(dest),
        }
    )
Path("/opt/usertest_maint_seed/manifest.json").write_text(
    json.dumps(seed_manifest, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY

RUN find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf {} +

WORKDIR /workspace
