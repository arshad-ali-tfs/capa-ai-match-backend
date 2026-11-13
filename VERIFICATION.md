# Local verification - 13 November 2026

The service was checked in its own Python 3.11 environment, with no installed platform-shared or sibling CAPA service package.

- The full MATCH suite passed 94 tests with real-model integration enabled, including all seven optional MPNet/Chroma checks. This includes Torch/ONNX vector agreement, real index construction, reopening in a fresh process and API retrieval. These are execution/parity checks, not a measured retrieval-accuracy improvement.
- After the final launcher/root adjustments, all 16 targeted import/configuration/build tests passed. The final default offline suite passed 88 tests, with seven optional real-model tests skipped; see .local/offline-tests.xml. Optional real-model tests are skipped by default, not reported as offline passes.
- pip check found no broken requirements. The Python wheel built successfully. The wheel was imported from an unrelated launch directory in isolated Python mode; its API health responded without access to any sibling package, and readiness correctly returned 503 without a configured source.
- Real standalone HTTP checks passed against the copied 4,292-record, 24,440-chunk index: one site, all 25 configured sites, score-component arithmetic, exact record IDs, details, source-version checks and forbidden-site rejection. See .local/live-match.json.
- The independent AUTHOR service retrieved results over HTTP and resolved the exact source record through its own copy of the same snapshot. See AUTHOR's cross-service-verification.json.
- AST comparison confirms eight retained engine modules are unchanged after namespace relocation; the compatibility API only loses its old standalone HTML route.

Reports, dependency provisioning details, model/index data, wheels and generated deployment bundles remain local under ignored paths. Only the named source CSV (Git LFS) and source notes are eligible under data/. Credentials and generated databases are excluded. These checks were completed before the initial GitHub publication. No Databricks deployment was attempted.

See DEPLOYMENT.md for target-environment requirements, including model/index provisioning, Linux compatibility, app permissions and actual query checks before promotion.
