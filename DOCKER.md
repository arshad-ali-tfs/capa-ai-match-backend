# MATCH Docker setup

Run from the repository root with Docker Desktop using Linux containers:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Match.ps1
```

This single command retrieves missing Git LFS assets, verifies the bundled source and pinned MPNet files, builds the image, imports the source, prepares the verified full Linux index (or builds one for another source), starts the backend and verifies single-site/all-sites searches. No host Python, separate model provisioning, manual index activation or OpenAI credentials are needed. Ordinary searches use the local model.

The detached service is available at http://127.0.0.1:8102/docs and continues after the terminal closes. The bundled source uses a verified index containing 4,292 records and 24,440 chunks. Source import and model readiness can take several minutes. A new source or explicit rebuild computes new embeddings and can take substantially longer, with chunk progress shown. The default indexes every record eligible under the existing DPD/status/record rules, using the unchanged MPNet ONNX FP32 model, fields, chunking and ranking.

## Updates and recovery

After pulling code, repeat the same command. It rebuilds changed Docker layers and reuses the completed index when its source, model and index code are compatible. Changed settings create a separate index. `-RebuildIndex` explicitly publishes a new generation while retaining previous files. Incomplete builds never replace an active generation; repeating a failed first setup retries safely.

```powershell
# Prepare the real source, model and index without starting the API.
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Match.ps1 -PrepareOnly

# Stop the managed container while retaining all data.
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Match.ps1 -Stop

# Use a separate installation with isolated data and another host port.
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Match.ps1 -ContainerName capa-ai-match-demo -Port 18102
```

`-Source "C:\data\export.xlsx" -Sheet "Sheet1"` imports another workbook; subsequent runs retain the chosen snapshot. `-Limit 5` builds a clearly limited pilot under a separate index identity. Repeat the same options when updating or stopping a custom installation. Other options are `-DataDirectory`, `-EnvFile`, `-Image` and `-BatchSize`.

## Configuration and storage

The launcher creates the ignored `.env.docker` if absent and preserves its settings. `MATCH_ALLOWED_SITES_JSON=auto` is a local setup choice for all indexed sites; an explicit JSON array restricts access and is never widened. An empty array grants no access and fails preparation. The API receives a resolved site array, not the word auto.

Resolved configuration and setup logs live under `.local/<container-name>/`. Persistent source snapshots, MPNet cache and versioned indexes live in the selected data directory, mounted at `/data`. Container-local paths are filled automatically. Do not put Windows paths into Docker configuration.

The service runs as UID/GID 10001. On Linux, grant this user access to mounted storage. Independent MATCH installations must use different writable data directories. A previous launcher container is upgraded only when its image name and exact data mount match this installation; unrelated containers are left untouched. Builds/preparation complete before the existing service is replaced.

The image contains service code and dependency pins. Source exports, model weights, indexes, local environments and credentials are excluded from the build context and supplied through mounts. The source, pinned model bundle and verified Linux index archive are versioned in Git/Git LFS; generated indexes and caches remain local. MPNet attribution is retained in `data/runtime/mpnet/README.md`.

The bundled index was checked against the bundled CSV: every indexed record, narrative chunk and CAPA link matches. The versioned runtime manifest records the two validated source identities: the original workbook snapshot and its canonical CSV import. Setup binds a fresh index copy to the selected validated identity, preserving original IDs and vectors. Other sources or incompatible index/model versions use the real builder. Do not edit an index manifest manually to bypass a source mismatch.

## Verify and diagnose

```powershell
docker logs --tail 50 capa-ai-match
docker exec capa-ai-match python -m capa_match.docker_setup verify
```

The entry command already runs this real-search verification. The regular container health probe proves process liveness; source readiness alone does not prove search readiness. A success message is printed only after the real index and both search scopes respond.

Setup logs include checksum, source, Docker and index build errors. No fake vectors or sample-result fallback is used. Git/repository access, Docker Desktop's Linux engine and enough local disk/memory remain prerequisites. Missing asset downloads require network access; indexing and search use the bundled model offline.

The three old PowerShell setup/start commands have been removed. `scripts/prepare_deployment.py` and `scripts/verify_local.py` remain administrative packaging and deeper verification tools, not extra setup steps. The Dockerfile can also be built by standard Docker tooling, including an approved `PYTHON_IMAGE` build argument.

This local Docker path is separate from the Databricks Apps deployment plan in [DEPLOYMENT.md](DEPLOYMENT.md). Target workspace storage, service identity, resource sizing and permissions still require deployment verification.
