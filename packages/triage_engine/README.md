# `triage_engine`

`triage_engine` contains small, reusable primitives for **triage and clustering**.

It is intentionally lightweight so it can be reused in different workflows:

- deduping issue titles
- building merge candidates
- clustering similar items
- extracting path anchors from text evidence

It is used by backlog tooling to merge/cluster tickets, but can be used independently.

---

## Install

Distribution name: `triage_engine`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

If you need only a runtime install (without dev tooling commands), use:

```bash
python -m pip install -e .
```

From a private GitLab PyPI registry (if you publish it):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "triage_engine==<version>"
```

> Publishing note
>
> This package is currently treated as **internal** unless opted into snapshot publishing via
> `[tool.monorepo].status` in `pyproject.toml`. See `docs/monorepo-packages.md`.

### Embeddings

`triage_engine` now defaults to OpenAI embeddings and fails fast if credentials are missing.

Required environment variables:

- `OPENAI_API_KEY`

Optional environment variables:

- `OPENAI_BASE_URL`
- `TRIAGE_ENGINE_OPENAI_MODEL` (default: `text-embedding-3-large`)
- `TRIAGE_ENGINE_EMBEDDER` with values `openai` or `openai:<model>`

There is no offline/local fallback in the default embedder path.

---

## Canonical smoke

Run from this package directory:

```bash
pdm run smoke
pdm run smoke_extended
```

`pdm run smoke` is the deterministic first-success check. `pdm run smoke_extended` performs a live
OpenAI embedding call when `OPENAI_API_KEY` is configured, otherwise it skips with a reason.

---

## Quickstart

```python
from triage_engine import cluster_items, dedupe_clusters, normalized_title

items = [
    {"id": "a", "title": "CLI crashes on Windows"},
    {"id": "b", "title": "Windows: CLI crash during install"},
    {"id": "c", "title": "Docs: clarify monorepo layout"},
]

clusters = cluster_items(
    items,
    get_title=lambda item: item["title"],
    get_text_chunks=lambda item: [item["title"]],
)
print([[items[idx]["id"] for idx in cluster] for cluster in clusters])

dedupe = dedupe_clusters(
    items,
    get_title=lambda item: item["title"],
    get_text_chunks=lambda item: [item["title"]],
)
print("dedupe clusters:", dedupe)

print(normalized_title("Windows: CLI crash during install"))
```

---

## Public API

Top-level exports:

- `cluster_items(items, ...)`
- `build_merge_candidates(items, ...)`
- `dedupe_clusters(items, ...)`
- `assess_trust(evidence, ...)`
- `compute_pair_similarity(a, b, ...)`
- `normalized_title(title)`
- `title_jaccard(a, b)`
- `tokenize(text)`
- `extract_path_anchors_from_chunks(chunks)`

---

## Development

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run smoke_extended
pdm run test
pdm run lint
```

`pdm run smoke_extended` runs a live OpenAI embedding smoke test and skips with an explicit reason when `OPENAI_API_KEY` is missing.

### Monorepo contributor workflow

Run from the monorepo root:

```bash
python tools/scaffold/scaffold.py run install --project triage_engine
python tools/scaffold/scaffold.py run test --project triage_engine
python tools/scaffold/scaffold.py run lint --project triage_engine
```
