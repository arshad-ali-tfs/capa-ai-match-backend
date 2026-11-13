# MATCH feature parity

The original retrieval logic is retained. Source-level AST comparison confirms eight engine modules are unchanged; only the compatibility API's standalone HTML route was removed. The primary API retains the platform's source-identity adapter and all-sites support. No scoring formula, embedding checkpoint, chunking algorithm or eligibility rule was changed.

| Capability | Implementation and validation |
|---|---|
| Closed DPD source eligibility and CAPA links | Retained ingest engine; source/import tests and actual 4,292-record index |
| Source IDs including leading zeros | Retained identity adapter, collision guards and search/detail tests |
| One-site and all-sites retrieval | Existing candidate union, configured site allowlist and API tests; real local searches |
| Semantic/BM25/Jaccard ranking | Retained 70/20/10 formula; component and combined-score assertions |
| Complete indexed record / original matched field | Site-scoped detail API; real source handoff to AUTHOR |
| Result limit and request validation | Original bounded top-k, explicit scope and API failure tests |
| Index/model metadata and readiness | Version, model/runtime, counts and mock notice retained |
| Index version mismatch / unavailable service | Safe 503 response, no stale source association |
| Rebuild controls and retained generations | Original division/site/model/runtime/batch/chunk/field/mock flags; build wrapper tests |
| CPU model runtimes | MPNet ONNX float32 and retained Torch path; optional real-model integration suite |
| Independent deployment | Own environment, imports, source copy, assets/cache, setup, entry point and wheel |

The old HTML search page is not a backend requirement and is not duplicated. The common frontend's MATCH design is pending. Test/demo fixtures remain under tests; production serving does not substitute them for an unavailable index.
