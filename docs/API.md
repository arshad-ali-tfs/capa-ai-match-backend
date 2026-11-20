# MATCH API reference

[Back to setup and architecture](../README.md)

Base URL locally: `http://127.0.0.1:8102`.

This reference covers **all 10 HTTP operations** for the current service entrypoint, including the experimental filtered explorer and cited AI overview. Each method/path pair counts once. This is a contract reference, not a measured retrieval-accuracy claim.

Use `/docs` for Swagger **Try it out**, `/redoc` for the alternate schema viewer and `/openapi.json` for the machine-readable contract. FastAPI also serves `/docs/oauth2-redirect` for Swagger authorization callbacks. These framework URLs are not business operations in the totals. Many handlers return dictionaries without a typed OpenAPI response; the response descriptions below explain those bodies. Download handlers return files even where the current schema shows a generic JSON response.

## Reading requests and errors

- Paths below are absolute paths on this service. Braced values are required path parameters; replace them and URL-encode identifiers.
- Unless a multipart body is specified, request bodies use `Content-Type: application/json`. When Body is none, send the request without a body.
- Invalid declared parameters/bodies produce HTTP **422** with FastAPI validation details. Runtime errors below add handler-specific behavior; unexpected infrastructure failures may still return **500**.
- Handler errors normally return `{"detail": "..."}`. HTTP success describes transport/execution, not approval or correctness of generated content.

## Endpoint index

### Service and source

| Method and path | Function |
|---|---|
| [`GET /api/v1/match/health`](#get-api-v1-match-health) | Check that the search API process responds. |
| [`GET /api/v1/match/ready`](#get-api-v1-match-ready) | Check the configured source snapshot. |
| [`GET /api/v1/match/capabilities`](#get-api-v1-match-capabilities) | Discover the service's declared feature flags. |
| [`GET /api/v1/match/source`](#get-api-v1-match-source) | Inspect source identity and row accounting. |

### Search and record inspection

| Method and path | Function |
|---|---|
| [`GET /api/v1/match/index`](#get-api-v1-match-index) | Check the loaded index, embedding configuration and searchable coverage. |
| [`GET /api/v1/match/sites`](#get-api-v1-match-sites) | List the configured sites that can be searched. |
| [`POST /api/v1/match/search`](#post-api-v1-match-search) | Find historical deviations similar to the query within the chosen site scope. |
| [`POST /api/v1/match/facets`](#post-api-v1-match-facets) | Authorized filter values, counts and eligible population. |
| [`POST /api/v1/match/overview`](#post-api-v1-match-overview) | Cited AI overview of a server-retained result set. |
| [`GET /api/v1/match/matched-records/{pr_id}`](#get-api-v1-match-matched-records-pr-id) | Inspect a complete indexed record within an allowed site. |

## Endpoint details

<a id="get-api-v1-match-health"></a>

### `GET /api/v1/match/health`

Check that the search API process responds.

**Body:** none.

**Returns (200):** Service name, alive status, environment mode and identity-proxy flag.

**Behavior and saved changes:** Does not load the search index or model.

**Errors:** No route-specific error mapping.

<a id="get-api-v1-match-ready"></a>

### `GET /api/v1/match/ready`

Check the configured source snapshot.

**Body:** none.

**Returns (200):** Source readiness and identity.

**Behavior and saved changes:** Source-only check; use /index to check search assets.

**Errors:** 503 when the source is unavailable.

<a id="get-api-v1-match-capabilities"></a>

### `GET /api/v1/match/capabilities`

Discover the service's declared feature flags.

**Body:** none.

**Returns (200):** Source browsing, business workflow, model-call and TrackWise connection flags.

**Behavior and saved changes:** Feature declaration, not an index or connectivity check. `optional_ai_assistance` is true; `model_calls` reports configured optional external AI assistance. Core embedding/retrieval is local.

**Errors:** No route-specific error mapping.

<a id="get-api-v1-match-source"></a>

### `GET /api/v1/match/source`

Inspect source identity and row accounting.

**Body:** none.

**Returns (200):** Snapshot provenance, accepted/quarantined counts and source metadata.

**Behavior and saved changes:** Describes the source snapshot; indexed/searchable coverage is reported separately by /index.

**Errors:** 503 when the source is unavailable.

<a id="get-api-v1-match-index"></a>

### `GET /api/v1/match/index`

Check the loaded index, embedding configuration and searchable coverage.

**Body:** none.

**Returns (200):** ready, model_name, embedding_backend/runtime, mock_embeddings, record/chunk counts, allowed_sites, text_fields, source_snapshot_id, ranking weights, division_scope and semantic_state (`warming`, `ready` or `unavailable`).

**Behavior and saved changes:** Loads/caches the engine if necessary, validates source identity and starts local query-model warm-up in the background. Does not build an index. Literal and browse searches remain usable while the model warms.

**Errors:** 503 for missing/incompatible index, source or model configuration.

<a id="get-api-v1-match-sites"></a>

### `GET /api/v1/match/sites`

List the configured sites that can be searched.

**Body:** none.

**Returns (200):** sites with allowed searchable deviation counts.

**Behavior and saved changes:** May initialize the engine; does not return unrestricted source sites.

**Errors:** 503 when the index/model configuration is unavailable.

<a id="post-api-v1-match-search"></a>

### `POST /api/v1/match/search`

Find historical deviations similar to the query within the chosen site scope.

**Body:** required; `application/json`; [SearchRequest](#searchrequest).

**Returns (200):** Ranked results with source IDs, matched field/text, score components, scope metadata and linked CAPA data.

**Behavior and saved changes:** Specify either site or all_sites=true, never both/neither. Core browse/literal searches need no model call. Ranked searches embed locally and reuse the existing index. `assist:true` can add external query interpretation/relevance review and a server-issued overview ID. It never rebuilds the index or writes source data.

**Errors:** 422 for schema/scope validation; 403 for a disallowed site; 400 for invalid search values; 503 for unavailable search assets.

<a id="post-api-v1-match-facets"></a>

### `POST /api/v1/match/facets`

Accepts the same scope and `filters` object as search; query/mode/paging fields are not used. Returns `source_snapshot_id`, normalized `filters`, `scope_record_count`, `eligible_record_count`, `missing_opened_dates` and `facets` for years, criticalities, dr_types and departments. Each facet item is `{value, count}`. Counts honor every other field but exclude that facet's own selection so multiple choices remain discoverable. They do not include the query text. Dates and CAPA constraints still apply. Site allowlists constrain all choices and counts. No model inference or saved mutation.

Errors: 422 for invalid filters/scope, 403 for a disallowed site, 503 for unavailable source/index assets.

<a id="post-api-v1-match-overview"></a>

### `POST /api/v1/match/overview`

Body: `{"search_id":"<32-lowercase-hex summary_id returned by assisted search>"}`. Extra fields are rejected. The server loads only the source rows retained for that search, within the current snapshot/authorized site set; callers cannot submit records or choose additional evidence.

Returns `search_id`, `source_snapshot_id`, `lines` (3–4 `{text, references}` objects), `reviewed_ids` (at most six), `returned_count`, `cached` and provider token usage. References are reviewed **deviation** IDs, not CAPA IDs. Successful results are reused for identical evidence, including repeated searches. Requests are sent to the configured Responses endpoint with `store:false` and bounded excerpts. No automatic provider retry loop is used; the UI offers a manual retry while source results remain available.

The current renderer receives two LLM synthesis sentences plus a server-calculated CAPA line. That line counts unique retained CAPA IDs, source Closed - Done statuses and reviewed deviations without retained links; narrative CAPA mentions cannot alter it. It may reference all six reviewed deviations. A longer AI draft gets at most one bounded evidence/length revision, reported as `usage.revision_pass`, without cutting a sentence mid-word. `MATCH_REVIEW_MODEL` defaults to `gpt-4.1`; query interpretation uses `MATCH_ASSIST_MODEL` (default `gpt-4.1-mini`).

Errors: 422 invalid request, 404 expired/unknown search or changed source/site authority, 403 denied source site, 503 AI/provider/validation failure. Search handles expire after 15 minutes; both handle and overview caches are bounded to 64 entries. Nothing is persisted to the source or index.

<a id="get-api-v1-match-matched-records-pr-id"></a>

### `GET /api/v1/match/matched-records/{pr_id}`

Inspect a complete indexed record within an allowed site.

| Parameter | Location | Type | Required / default | Constraints |
|---|---|---|---|---|
| `pr_id` | path | string | **Required** | - |
| `site` | query | string | **Required** | - |

**Body:** none.

**Returns (200):** Full matched record and source CAPA relationships. Optional `literal_field_texts.INVESTIGATION` contains unmodified investigation text from the matching snapshot; it is separate from `indexed_field_texts` because the current semantic vectors do not include that field.

**Behavior and saved changes:** The site query parameter is required even if a previous search covered all sites. There is no unscoped record-browsing route.

**Errors:** 422 for missing site; 403 for a disallowed site; 404 when the record is absent from that site; 503 for unavailable engine configuration.

## Request bodies

Required/default columns describe omission, not automatic approval or workflow readiness. Array/object factory defaults are shown explicitly where the runtime uses them.

<a id="searchrequest"></a>

### SearchRequest

| Field | Type | Required / default | Constraints |
|---|---|---|---|
| `query` | string | `""` | max characters 30000; blank browses; nonblank requires at least two letters/numbers except an exact source ID |
| `site` | string or null | `null` / omitted | min characters 1 |
| `all_sites` | boolean | `false` | - |
| `top_k` | integer | `10` | min 1; max 50 |
| `mode` | string | `auto` | `auto`, `keyword`, `exact`, `semantic` |
| `filters` | object | empty filters | See below; unknown properties rejected |
| `offset` | integer | `0` | 0–1,000,000; browse/literal pagination only |
| `assist` | boolean | `false` | Opt into optional query assistance, relevance review and overview handles; existing clients are unchanged |

Filter fields: `years` (integer array, 1900–2200, at most 100), `criticalities` (string array, at most 20), `dr_types` (string array, at most 50), `departments` (string array, at most 100), `date_from` / `date_to` (ISO dates or null, inclusive, from ≤ to), `capa` (`any`, `linked`, `completed`, `none`). Strings must be nonempty after trimming and at most 200 characters. Multiple choices in one field mean OR; different fields mean AND. Missing opened dates are excluded when a year or date constraint is active. Completed requires a retained linked CAPA marked Closed - Done; none means no retained link, not proof that no CAPA exists.

Explorer responses add `requested_mode`, resolved `search_mode`, normalized `filters`, `scope_record_count`, `eligible_record_count`, `required_identifiers`, `offset`, `limit`, `has_more` and `total_matches`. Browse/literal searches return a complete count and null scores. Ranked searches return `total_matches: null` (not an exhaustive match count), no pagination, up to `top_k` candidates, `relevance_score`, `event_title_score` and `ranking_explanation`. Rows include `match_kind`, factual `match_reasons` and zero-based Unicode-codepoint `matched_spans` into `matched_excerpt`. Clients using UTF-16 must convert offsets before highlighting.

Always send `mode` explicitly to opt into the experiment. For compatibility, a narrative of at least eight characters that omits all three of `mode`, `filters` and `offset` follows the original hybrid contract used by AUTHOR. Explicitly requesting `auto` selects the new explorer even when other values are defaults.

With `assist:true`, `submitted_criteria` echoes the original validated request; `query` remains the original user input. Actual `site`/`filters` may include explicit natural-language constraints, and `interpretation` reports `{query, origin}` (`llm` or local fallback). An empty interpreted query means filter-only browsing even when the original input is nonempty. Selected manual filters always take precedence. IDs remain hard constraints even in long descriptions; explicit Meaning mode permits broader comparisons.

Assisted natural-language candidates combine original/clarified retrieval positions (RRF constant 60). Optional LLM relevance review considers up to 20 candidates, returns only supplied IDs, and can remove incidental matches. Displayed review quotes must occur in that record's supplied title/description/matched excerpt. `candidate_relationship` is `direct` or `related`, an AI assessment rather than a validated label. `rank_fusion:true` tells the frontend to show order and evidence instead of a misleading numeric confidence. Empty ranked results remain non-exhaustive (`total_matches:null`).

`summary_id` is a short-lived opaque overview handle, or null for empty results. `assistant_usage` and optional `relevance_review_usage` report provider tokens; cache reuse can omit a new usage record. A provider failure retains local retrieval, explicit constraints and source access. Plain exact identifiers, quoted phrases and manual search modes bypass query rewriting. If `assist` is false, no new AI call or overview handle is created.

## Search scope and inputs

| Field | Function |
|---|---|
| `query` | Blank for browsing; deviation ID, equipment/document reference, keywords, phrase or problem narrative. |
| `site` | Exact site name from the allowed-sites endpoint for a single-site search. |
| `all_sites` | Set `true` to search every indexed site allowed by server configuration. This does not override the allowlist. |
| `top_k` | Maximum returned ranked records; default 10, allowed 1-50. |

Scope must be **either** a non-empty `site` **or** `all_sites: true`. Sending both, or sending neither, fails request validation. HTTP clients must preserve the site's exact name and URL-encode it when used in a record-detail query.

## Client examples

POST to the search endpoint for all allowed sites:

```json
{"query": "HPLC bracket standard failed system suitability", "all_sites": true, "mode":"auto", "top_k": 10}
```

For one site, replace the placeholder with an exact value returned by the sites endpoint:

```json
{"query": "HPLC bracket standard failed system suitability", "site": "<exact allowed site>", "mode":"auto", "top_k": 10}
```

To open a returned record, GET `/api/v1/match/matched-records/<returned-pr-id>?site=<URL-encoded-site>`. The site is required again; it is not remembered from the previous request.

For filtered browsing:

```json
{"query":"","all_sites":true,"mode":"auto","filters":{"years":[2025,2026],"criticalities":["Major","Critical"]},"top_k":25,"offset":0}
```

Smart numeric input (or `PR10647`) means exact deviation ID. Short identifier-led searches require all detected identifiers, allowing separator variants but preserving digits and leading zeros. `HPLC` is a literal keyword; `"line clearance"` is a literal phrase. Keywords mode requires all terms across the retained fields. Longer Smart narratives use hybrid ranking; Meaning mode removes hard identifier constraints. Description/conclusion/title plus available investigation text support literal searches. The current semantic vectors remain description/conclusion only; recorded titles are compared during reranking, investigation text is not semantically embedded.

Explorer relevance combines 70% narrative rank and 30% recorded event-title meaning among up to three times the requested candidates, capped at 50. Narrative hybrid rank retains semantic/BM25/Jaccard weights 70/20/10; Meaning narrative rank uses semantic similarity only. When a title is missing, narrative rank is used alone. Original components and `match_score` stay available separately. Literal/browse rows have null scores. These are review-ranking signals, not probabilities of correctness or recurrence. Check `/index` for source version, text-field coverage and mock embeddings.

## Provisioning and readiness

`/health` checks the process. `/ready` checks the imported source. `/capabilities` declares features. `/index` loads/checks the actual search assets and their source identity. A successful source check alone does not establish that search can run.

The API reuses a prepared index. Importing source files and building/rebuilding an index are CLI/setup operations, not missing HTTP endpoints. MATCH has no case-write, annotation, unscoped `/records` browsing, or OpenAI-generation API.

## Compatibility and inventory scope

The current entrypoint is `capa_match.main:app`. Older routes defined inside the retained `match_v1.api` module, such as `/api/search`, are not mounted on this application and are not supported aliases here. Use the ten operations above when integrating this service. AI assistance/overviews belong to this source-backed entrypoint, not the retained standalone compatibility API.

Implementation references: [OpenAI structured Responses](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses) and [reciprocal rank fusion](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion).

## Implementation references

- [HTTP search and record handlers](../src/capa_match/main.py)
- [Source, readiness and capabilities](../src/capa_match/infrastructure/web.py)
- [Search request and site-scope validation](../src/match_v1/contracts.py)
