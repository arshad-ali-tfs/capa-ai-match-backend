# Local verification - 13 November 2026

## Docker packaging verification - 16 November 2026

- Built and ran `capa-ai-match:local` on Docker Desktop 4.99.0, Linux/amd64, Python 3.11.18, as UID/GID 10001. The existing dependency lock passed `pip check`; model and business code were not changed.
- Eight targeted offline packaging checks passed, including simulated first-use builds, reuse of an existing image, native progress on stderr, failed-build handling, paths with spaces, custom ports and local HTTP probes. See `.local/docker-runtime-tests.xml`. The identical launcher logic was also exercised against the real Docker engine, through automatic image building and API startup.
- Source exports, credentials, databases, model assets and generated outputs are excluded from the image. Runtime configuration stays in the ignored `.env.docker`; persistent state is bind-mounted. Test writes used isolated storage and left the original source and case/run artifacts intact.
- A separate copy of the existing index opened successfully in Linux: 4,292 records, 24,440 chunks, 25 sites, real MPNet ONNX FP32 embeddings. One-site/all-site search, exact record details, source identity, score arithmetic and forbidden-site rejection passed. The same index reopened and returned results after a container restart. This was a validated copy, not an index rebuild. See `.local/docker-validation/match-checks.json`.
- AUTHOR retrieved three real semantic results over the private Docker network. Both services reported the same source snapshot and 19,643 accepted source rows. Cold index/model readiness took about 124 seconds on this workstation; health alone proves only API liveness.
- These are local Linux execution and persistence checks, not accuracy improvements or cloud deployment validation. Docker images were built locally; registry publication and cloud deployment remain separate steps. Continue with the environment-specific checks in [DOCKER.md](DOCKER.md) and [DEPLOYMENT.md](DEPLOYMENT.md) before deployment.

The service was checked in its own Python 3.11 environment, with no installed platform-shared or sibling CAPA service package.

- The full MATCH suite passed 94 tests with real-model integration enabled, including all seven optional MPNet/Chroma checks. This includes Torch/ONNX vector agreement, real index construction, reopening in a fresh process and API retrieval. These are execution/parity checks, not a measured retrieval-accuracy improvement.
- After the final launcher/root adjustments, all 16 targeted import/configuration/build tests passed. The final default offline suite passed 88 tests, with seven optional real-model tests skipped; see .local/offline-tests.xml. Optional real-model tests are skipped by default, not reported as offline passes.
- pip check found no broken requirements. The Python wheel built successfully. The wheel was imported from an unrelated launch directory in isolated Python mode; its API health responded without access to any sibling package, and readiness correctly returned 503 without a configured source.
- Real standalone HTTP checks passed against the copied 4,292-record, 24,440-chunk index: one site, all 25 configured sites, score-component arithmetic, exact record IDs, details, source-version checks and forbidden-site rejection. See .local/live-match.json.
- The independent AUTHOR service retrieved results over HTTP and resolved the exact source record through its own copy of the same snapshot. See AUTHOR's cross-service-verification.json.
- AST comparison confirms eight retained engine modules are unchanged after namespace relocation; the compatibility API only loses its old standalone HTML route.

Reports, dependency provisioning details, model/index data, wheels and generated deployment bundles remain local under ignored paths. Only the named source CSV (Git LFS) and source notes are eligible under data/. Credentials and generated databases are excluded. These checks were completed before the initial GitHub publication. No Databricks deployment was attempted.

See DEPLOYMENT.md for target-environment requirements, including model/index provisioning, Linux compatibility, app permissions and actual query checks before promotion.
