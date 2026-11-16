# MATCH Docker image

This repository builds one independent Linux/amd64 image using Python 3.11 and the existing dependency lock. It starts the existing MATCH launcher, without changing models, prompts or business workflows. Other backend repositories are not build inputs.

## Prepare once

Install Docker Desktop with its Linux engine (or Docker Engine on Linux). From this repository:

~~~powershell
Copy-Item docker.env.example .env.docker
~~~

Edit `.env.docker` with container paths and service settings. For example, a host file under `data/sources/` appears under `/data/sources/` in the container. Do not copy Windows paths or shell quotes into this file. Keys stay in this ignored local file and are injected at runtime.

Fetch the source CSV with `git lfs pull` when using the bundled source. Prepare the snapshot and service assets described below. The build context excludes source exports, databases, model caches, runs, local environments, Git history and credentials.

## One command to start this service

~~~powershell
.\Start-Docker.ps1
~~~

The launcher builds `capa-ai-match:local` when it is missing, creates/reuses the `capa-ai` network, mounts this repo's `data` directory at `/data`, and starts only MATCH. It runs as UID/GID 10001, binds the application to `0.0.0.0:8102` inside Docker, and publishes it on `127.0.0.1:8102` on the laptop.

API documentation: http://127.0.0.1:8102/docs. Stop with Ctrl+C. The container is removed on exit; mounted data is retained.

After changing code, rebuild explicitly:

~~~powershell
.\Start-Docker.ps1 -Build
~~~

If the native backend already uses 8102, choose another host port:

~~~powershell
.\Start-Docker.ps1 -Port 18102
~~~

`-DataDirectory`, `-EnvFile` and `-Image` also accept overrides. Keep separate writable data directories for different services. On Linux, grant UID/GID 10001 access to the mounted data directory, or use `docker run --user` with a suitable host UID/GID. A mounted directory's permissions override the image directory's permissions.

## Build and run without the Windows helper

~~~bash
docker build --platform linux/amd64 --tag capa-ai-match:local .
docker network create capa-ai
docker run --rm --init --platform linux/amd64 --name capa-ai-match --network capa-ai --publish 127.0.0.1:8102:8102 --env-file .env.docker --mount "type=bind,source=$(pwd)/data,target=/data" capa-ai-match:local
~~~

Create the network only once; reuse it if it already exists. The Dockerfile also accepts `--build-arg PYTHON_IMAGE=...` to use an approved Python 3.11 Debian Bookworm image, including a digest-pinned image.

## Source and persistent storage

For an existing local installation, reuse its `data` folder and set `CAPA_SOURCE_SNAPSHOT=/data/sources/<actual-snapshot-id>.sqlite3` and `CAPA_SOURCE_CSV=/data/sources/<actual-snapshot-id>.csv`. The three services should reference the same source snapshot identity.

For a fresh folder, build the image first, place the CSV in `data/sources/trackwise_records.csv`, then import it using the existing service command:

~~~powershell
docker build --platform linux/amd64 --tag capa-ai-match:local .
$dataPath = (Resolve-Path .\data).Path
docker run --rm --env-file .env.docker --mount "type=bind,source=$dataPath,target=/data" capa-ai-match:local match-import /data/sources/trackwise_records.csv
~~~

Use `--sheet Sheet1` for an XLSX workbook as needed. The import prints the snapshot ID. Set the two `CAPA_SOURCE_*` paths in the host `.env.docker` to the generated `/data/sources/<id>.sqlite3` and `/data/sources/<id>.csv` files. Import commands write a container-local `/app/.env`; that file is temporary and does not update the host configuration automatically.

Mount the MPNet cache at `/data/models/huggingface`. On this workstation, a separate copy of the existing index at `/data/match-index-linux` passed real Linux queries and reopened after a restart. It was not rebuilt. For another installation, validate a separate copy first or build a new Linux index; do not assume Windows Chroma/HNSW files are portable:

~~~powershell
$dataPath = (Resolve-Path .\data).Path
docker run --rm --env-file .env.docker --mount "type=bind,source=$dataPath,target=/data" capa-ai-match:local match-build --index-dir /data/match-index-linux
~~~

Set `MATCH_INDEX_DIR=/data/match-index-linux`. Set `MATCH_ALLOWED_SITES_JSON` to a plain JSON array containing the approved keys from that directory's `manifest.json` `site_counts` object. Index activation writes a temporary container-local `/app/.env`; copy these settings to the host `.env.docker` before starting the server. Keep the existing checkpoint, text fields and ranking settings. The image includes CPU-only Torch at the existing version. A first model download requires explicitly setting `HF_HUB_OFFLINE=0` and `TRANSFORMERS_OFFLINE=0`; switch them back after provisioning the cache.

The source snapshot, CSV, model cache and index generation must agree. Do not rebuild an index while a running server is using it. An empty allowed-site list grants no site access.

## Verify

~~~powershell
docker inspect --format '{{json .State.Health}}' capa-ai-match
docker exec capa-ai-match python /opt/capa/healthcheck.py --path /api/v1/match/index --timeout 180
~~~

The built-in Docker health check probes only `/api/v1/match/health`, which proves the API process is alive. The explicit command above checks index/model readiness and source compatibility; also run an actual allowed-site search. Missing source data or model assets produce an unavailable response rather than sample results.

This Docker configuration is independent of the Databricks Apps `app.yaml` source-deployment path in [DEPLOYMENT.md](DEPLOYMENT.md). Keep the existing identity, storage and target-environment validation steps for deployment. No Compose file is required.
