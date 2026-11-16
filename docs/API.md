# MATCH API reference

[Back to setup and architecture](../README.md)

Base URL locally: `http://127.0.0.1:8102`.

This reference covers **all 8 HTTP operations** in the generated OpenAPI schema for the current service entrypoint. Each method/path pair counts once. Request field types, defaults and constraints below come from that schema; purpose, persistence and runtime errors were checked against the handlers. This is a contract audit, not a live model or accuracy test.

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

**Behavior and saved changes:** Static feature declaration, not an index or connectivity check. MATCH makes no OpenAI calls.

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

**Returns (200):** ready, model_name, embedding_backend/runtime, mock_embeddings, record/chunk counts, allowed_sites, text_fields, source_snapshot_id and ranking weights.

**Behavior and saved changes:** Loads/caches the engine if necessary and validates that the index belongs to the configured source. Does not build an index.

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

**Behavior and saved changes:** Specify either site or all_sites=true, never both/neither. Embeds the query locally and searches the existing index; no OpenAI call or index rebuild.

**Errors:** 422 for schema/scope validation; 403 for a disallowed site; 400 for invalid search values; 503 for unavailable search assets.

<a id="get-api-v1-match-matched-records-pr-id"></a>

### `GET /api/v1/match/matched-records/{pr_id}`

Inspect a complete indexed record within an allowed site.

| Parameter | Location | Type | Required / default | Constraints |
|---|---|---|---|---|
| `pr_id` | path | string | **Required** | - |
| `site` | query | string | **Required** | - |

**Body:** none.

**Returns (200):** Full matched record and source CAPA relationships.

**Behavior and saved changes:** The site query parameter is required even if a previous search covered all sites. There is no unscoped record-browsing route.

**Errors:** 422 for missing site; 403 for a disallowed site; 404 when the record is absent from that site; 503 for unavailable engine configuration.

## Request bodies

Required/default columns describe omission, not automatic approval or workflow readiness. Array/object factory defaults are shown explicitly where the runtime uses them.

<a id="searchrequest"></a>

### SearchRequest

| Field | Type | Required / default | Constraints |
|---|---|---|---|
| `query` | string | **Required** | min characters 8; max characters 30000 |
| `site` | string or null | `null` / omitted | min characters 1 |
| `all_sites` | boolean | `false` | - |
| `top_k` | integer | `10` | min 1; max 50 |

## Search scope and inputs

| Field | Function |
|---|---|
| `query` | Problem narrative to compare with indexed records; 8-30,000 characters. |
| `site` | Exact site name from the allowed-sites endpoint for a single-site search. |
| `all_sites` | Set `true` to search every indexed site allowed by server configuration. This does not override the allowlist. |
| `top_k` | Maximum returned ranked records; default 10, allowed 1-50. |

Scope must be **either** a non-empty `site` **or** `all_sites: true`. Sending both, or sending neither, fails request validation. HTTP clients must preserve the site's exact name and URL-encode it when used in a record-detail query.

## Client examples

POST to the search endpoint for all allowed sites:

```json
{"query": "HPLC bracket standard failed system suitability", "all_sites": true, "top_k": 10}
```

For one site, replace the placeholder with an exact value returned by the sites endpoint:

```json
{"query": "HPLC bracket standard failed system suitability", "site": "<exact allowed site>", "top_k": 10}
```

To open a returned record, GET `/api/v1/match/matched-records/<returned-pr-id>?site=<URL-encoded-site>`. The site is required again; it is not remembered from the previous request.

Search responses carry the ranked records and their source IDs, matched narrative field/text, component scores and CAPA relationships. Check `/index` for current model, text-field coverage, source version and whether mock embeddings are enabled. Scores combine semantic, BM25 and Jaccard signals (70/20/10); they are ranking values, not percentages of correctness.

## Provisioning and readiness

`/health` checks the process. `/ready` checks the imported source. `/capabilities` declares features. `/index` loads/checks the actual search assets and their source identity. A successful source check alone does not establish that search can run.

The API reuses a prepared index. Importing source files and building/rebuilding an index are CLI/setup operations, not missing HTTP endpoints. MATCH has no case-write, annotation, unscoped `/records` browsing, or OpenAI-generation API.

## Compatibility and inventory scope

The current entrypoint is `capa_match.main:app`. Older routes defined inside the retained `match_v1.api` module, such as `/api/search`, are not mounted on this application and are not supported aliases here. Use the eight operations above when integrating this service.

## Implementation references

- [HTTP search and record handlers](../src/capa_match/main.py)
- [Source, readiness and capabilities](../src/capa_match/infrastructure/web.py)
- [Search request and site-scope validation](../src/match_v1/contracts.py)
