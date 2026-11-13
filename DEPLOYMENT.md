# Databricks Apps deployment plan: MATCH

This repository runs locally as one independent FastAPI service. app.yaml, dependency pins and a code-only staging command are included. No Databricks workspace deployment or Linux runtime validation has been performed here. The common frontend is deployed separately; its current design integrates MAP only.

## Prepare and verify locally

1. Use Python 3.11 and this folder as the repository root. Run Setup-Match.ps1, configure local .env and start Start-Match.ps1. Follow README.md for source/model provisioning.
2. Run the offline tests, pip check and the real-service smoke script described in VERIFICATION.md. For MATCH, rebuild an index after changing the source; never reuse an unrelated generation. For AUTHOR, verify cases and originals after restarting the service.
3. Fetch the included CSV with Git LFS. The source export is versioned; database snapshots, indexes, models, credentials and generated cases are ignored. Publish this folder as its own private GitHub repository only when requested.
4. Prepare a clean upload from this repository root:

```powershell
.\.venv\Scripts\python.exe scripts/prepare_deployment.py
```

The command prints a directory under .local/deployment. It includes the service code, app.yaml, dependency lock and documentation, with a SHA-256 manifest. Source CSVs, databases, models, local .env, dependencies, caches and test output are excluded. The staged requirements install the Databricks SDK extra. Do not upload the entire working folder or Git LFS dataset as app-source files.

## Provision the environment

5. Create a separate Databricks App for this backend and upload/sync the prepared code directory. requirements.txt selects pip installation; app.yaml starts python -m capa_match.cli, binds 0.0.0.0 and lets the runtime supply DATABRICKS_APP_PORT. The configured health check must succeed before promotion. This follows [App execution configuration](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/app-runtime) and [dependency installation](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/dependencies).
6. Store the source snapshot and model/index artifacts outside app source. Grant the service principal read access to its required [Unity Catalog volume resources](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/uc-volumes). CAPA_SOURCE_SNAPSHOT accepts an immutable local SQLite file or a volume URI; the service stages volume files into CAPA_CACHE_DIR using the SDK when necessary. An SDK-cached source is read-only service input, not the writable case store. Keep all services on the same snapshot ID when connecting them.

7. Build a compatible MATCH index in the target Linux environment using the included builder and the same MPNet checkpoint, runtime and source snapshot. Prefer rebuilding over assuming Windows Chroma/HNSW binary files are portable. Keep the index immutable while serving. Package the active manifest plus its referenced generation at the archive root, and configure MATCH_INDEX_DIR with the local directory or volume ZIP URI. Package the Hugging Face cache separately (models--sentence-transformers--all-mpnet-base-v2 at the archive root); MATCH_MODEL_CACHE accepts its local directory or ZIP URI. Both archives can be downloaded to the app's local cache. Set MATCH_MODEL_NAME, MATCH_ALLOWED_SITES_JSON, HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 explicitly. Size the app for the measured cold-start and MPNet/Chroma memory needs.
8. Validate the health check GET /api/v1/match/index, then execute scripts/verify_local.py against the deployed URL. Confirm the actual model loads, the source version agrees, both site scopes work, record IDs remain exact, score components remain intact and forbidden sites return 403. The source-only /ready endpoint does not prove a model has loaded; an actual query is required.
9. Grant the future frontend and AUTHOR app principals CAN USE on this MATCH App through an [app resource](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/apps-resource). Keep site authorization at the service boundary; the local static allowlist does not implement identity-based row-level access.

The app only serves a published index. Rebuilding/replacing generations is an administrative operation, not an HTTP endpoint. For replicas, give each process its own writable download/cache directory and the same immutable published artifacts. Do not have multiple servers write one local Chroma directory.

## Frontend integration and promotion

The common React application remains a separate repository. Its current proxy exposes MAP only. When the MATCH/AUTHOR frontend designs arrive, add the service-specific proxy/API client there and validate its app-resource permissions; this backend task adds no UI screens. Use the OpenAPI schema as the integration contract.

Promote only after the staged package installs on target Linux/Python, app resources and secrets are configured, cold-start and working-set measurements fit the selected app resources, the real-service checks pass, and persistent data survives restart. A running local prototype or passing unit suite alone is not cloud validation.
