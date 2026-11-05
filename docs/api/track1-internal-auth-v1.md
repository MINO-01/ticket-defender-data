# Track 1 internal authentication v1 (Issue #26)

Status: IMPLEMENTED in ticket-defender-data. The Backend side is NOT implemented yet. Track 1 integration is complete only after the Backend issue that sends `X-TD-Track1-Key` is merged and the joint integration tests below pass.

## Scope

- Protected: `/api/v1/clusters/analyze`, every HTTP method. The trailing-slash form and the `root_path`-prefixed form are also protected.
- Disabled: `/api/v1/clusters/macro`, which always returns HTTP 410 (see below).
- Not affected: the Track 2 VLM routes `/api/v1/parse/ticket-image` and `/api/v1/parse/listing-context`. They keep `X-TD-Internal-Key`, `VLM_INTERNAL_SHARED_SECRET` and their own pre-body gate. The two gates match disjoint paths.

## Contract

| Item | Value |
| --- | --- |
| Request header | `X-TD-Track1-Key` (exactly one occurrence) |
| Server environment variable | `TRACK1_INTERNAL_SHARED_SECRET` |
| Minimum secret length | 32 bytes, counted in UTF-8 |
| Secret separation | Must differ from `VLM_INTERNAL_SHARED_SECRET`. An equal value is treated as misconfiguration (HTTP 503). |
| Comparison | `hmac.compare_digest` over the raw header bytes |
| Fallbacks | None. `Authorization: Bearer`, `X-TD-Internal-Key`, query parameters and cookies are not accepted. No option exists to disable the check. |

The gate is a pure ASGI middleware (`core/track1_security.py`). It runs before the request body is received, so before JSON parsing, Pydantic validation, dependency injection and `get_graph_service`. A rejected request never opens a Neo4j session and never runs Louvain.

Evaluation order:

1. Secret missing, shorter than 32 UTF-8 bytes, or equal to the Track 2 secret: HTTP 503.
2. Header missing, repeated, or not equal to the secret: HTTP 401.
3. Otherwise the request reaches the existing `/analyze` handler unchanged.

Logs record only the failure type. The secret, the supplied header value and request identifiers are never logged.

## Error responses

The bodies follow the existing Track 1 error shape `{"detail": [{"code": ...}]}` used by the `/analyze` 422 responses. They do not use Track 2 `application/problem+json`, so that Track 1 errors stay uniform for the Spring consumer. Responses carry `Cache-Control: no-store`.

HTTP 401 (header missing, duplicated or wrong):

```json
{"detail": [{"code": "INTERNAL_AUTH_REQUIRED"}]}
```

HTTP 503 (server secret not configured or invalid):

```json
{"detail": [{"code": "TRACK1_AUTH_UNCONFIGURED"}]}
```

HTTP 410 (`/api/v1/clusters/macro`, any credential or none):

```json
{"detail": [{"code": "LEGACY_ENDPOINT_REMOVED"}]}
```

None of these is an analysis result. A caller must never map 401, 503 or 410 to `COMPLETED` with zero clusters.

## Behaviour after authentication

A request with a valid key is processed exactly as before. The `Track1AnalysisRequest` request body and the `Track1AnalysisResponse` response (`requestId`, `status`, `algorithm`, `algorithmVersion`, `analyzedAt`, `clusters`) are unchanged. The existing 422 validation codes and the 500/503 analysis failure responses are unchanged.

## Legacy `/api/v1/clusters/macro`

The route previously accepted unauthenticated requests, wrote `Account`, `Payment`, `Address`, `Device` and `IP` nodes into Neo4j with `MERGE`, and ran WCC. No caller exists in the Data, Backend or Frontend repositories. Spring `FastApiAdapter` calls only `/analyze`.

The handler is now a parameterless stub that returns 410. It does not read the body, does not resolve `get_graph_service`, and is hidden from the OpenAPI schema. An explicit 410 was chosen over removing the route (404) because specs `00` AUD-001 requires the legacy path to be managed explicitly rather than silently dropped or redirected. Adding authentication to `/macro` was rejected because it would keep the unnecessary persistent graph write reachable.

`GraphService.load_json_to_graph`, `GraphService.detect_macro_clusters` and the legacy DTOs remain in the code but are unreachable over HTTP. Removing them is a separate follow-up, because it touches WCC code outside this issue.

## Difference from the specs proposal

`ticket-defender-specs/contracts/track1-internal-v1-proposal.yaml` declares `internalServiceAuth` as `type: http, scheme: bearer` and describes it as "proposed, not yet verified". This implementation uses the dedicated header `X-TD-Track1-Key` instead, for three reasons:

- it mirrors the existing, tested Track 2 `X-TD-Internal-Key` pattern;
- a distinct header name prevents a Track 2 credential from being accepted on Track 1;
- `Authorization` is left free for a future infrastructure-issued credential.

The proposal also lists only 401 and 503 as authentication-related responses. The 503 here has a specific meaning: the server's own secret is missing or invalid. The specs proposal should be updated in a separate, authorized specs change. This repository does not edit specs.

## Backend changes required

Until Spring sends the header, every Track 1 call from the current `FastApiAdapter` receives **HTTP 401** from a Data service that includes this change. `RestClient` raises `HttpClientErrorException.Unauthorized`, which is a `RestClientException`. That exception reaches the `@CircuitBreaker` fallback `analyzeFallback(MacroAnalysisRequest, Throwable)`, which returns `MacroAnalysisResponse.unavailable(requestId)`. So the analysis is reported as unavailable, not as zero findings. It is still a total Track 1 outage, and a deploy of Data alone does not complete the integration.

Required in `ticket-defender-backend` (separate Backend issue):

1. Read the secret from configuration bound to the environment variable `TRACK1_INTERNAL_SHARED_SECRET` (the same name on both services). Fail application startup, or fail the call before sending, if it is missing or shorter than 32 UTF-8 bytes. Never log it.
2. In `FastApiAdapter.requestMacroAnalysis`, add exactly one header to the `/api/v1/clusters/analyze` request: `.header("X-TD-Track1-Key", track1Secret)`.
3. Do not reuse the Track 2 VLM secret. Do not add `Authorization` or any second credential.
4. Keep treating 401, 503 and 410 as failures (unavailable or error), never as `COMPLETED` with empty clusters. Consider surfacing 401 and 503 as a configuration alert, not a transient outage, because retries will not fix them.

## Deployment order

Data and Backend must be released together with the same secret value:

1. Generate one random secret of at least 32 bytes (for example 32 random bytes, hex or base64 encoded). It must be different from `VLM_INTERNAL_SHARED_SECRET`.
2. Store it in both deployments' secret stores as `TRACK1_INTERNAL_SHARED_SECRET`.
3. Deploy the Backend change that sends the header. The current Data release ignores the extra header.
4. Deploy this Data change.

If Data is deployed first, Track 1 returns 401 until Backend is updated. If the secret is missing on Data, Track 1 returns 503. Neither condition produces analysis results.

## Minimum integration tests for the switch

Run against an isolated Data instance with synthetic tickets and a fake or isolated graph:

1. Backend with the correct key: `/analyze` returns 200 and passes `FastApiAdapter.validateResponse`.
2. Backend without the header, or with a wrong key: Data returns 401, and Backend records the analysis as unavailable or failed, not as zero clusters.
3. Data without `TRACK1_INTERNAL_SHARED_SECRET`: 503, and Backend records it as unavailable or failed.
4. Backend sends a duplicated header: 401.
5. Track 2 calls still authenticate with `X-TD-Internal-Key` only.
6. No secret value appears in Backend or Data logs.

Data-side coverage of the contract lives in `tests/test_track1_internal_auth.py` and `tests/test_track1_analysis.py`.
