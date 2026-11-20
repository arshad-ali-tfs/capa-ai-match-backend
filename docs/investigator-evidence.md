# Source fidelity and investigation evidence

The `scientific-text-v2` normalizer removes recognized HTML tags while retaining scientific comparisons and angle-wrapped narrative. Rebuild an older index before serving this branch. `python -m capa_match.build --rebuild` uses the configured index directory when its snapshot matches. A new generation activates only after successful validation; byte-identical text vectors can be reused when the embedding model/runtime/dimension contract matches. Older generations are retained.

Search filters support multiple included/excluded sites, status, included/excluded criticalities and opened years, date boundaries, DR types, departments and retained CAPA links. Natural-language scope is resolved before query assistance; explicit filter controls take precedence. Relative dates have a visible as-of date. The supplied index contains closed historical deviations, so an open-deviation request returns no closed substitutes.

Record detail returns original source fields independently of the retrieval index. Review and overview prompts use bounded original passages. Each accepted AI overview sentence requires original supporting quotations for its cited records. Linked CAPA status is calculated independently. Provider failure or unverified review is reported as local ranking. Neither ranking nor grounding is a formal investigation approval.

`GET /api/v1/match/cases` lists retained investigations. `POST /api/v1/match/cases` accepts a server-issued `search_id`, title, reviewer, notes and up to six include/exclude decisions with rationale. The search must still be in the server cache (15 minutes). Candidate IDs are validated against that search; the server obtains the original evidence. `GET /api/v1/match/cases/{id}` reopens the frozen bundle after cache expiry. Current allowed-site restrictions apply on save/list/read. Bundles retain source/index identity, model names and a prompt/schema recipe hash. Source or index changes are flagged without replacing saved evidence.

Data is written only under `CAPA_DATA_DIR/match-cases`. The source snapshot is read-only. These local evidence files do not update a QMS. Attachments and approved effectiveness findings cannot be reconstructed when absent from the supplied export.

Run `python -m pytest -q` for the offline contract, retrieval, source-integrity, scope, grounding and persistence checks. Provider tests use controlled responses; real-provider checks must use the configured authorized endpoint and test runtime.
