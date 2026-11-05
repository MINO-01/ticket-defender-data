# Track 2 live VLM extraction evaluation

Issue #23 adds a separate, opt-in harness that sends **synthetic** listing images to the real Gemini API through the existing `POST /api/v1/parse/listing-context` route, then scores the nine observation fields against fixed answers. It reuses `GeminiProvider`, `VLMService`, the Pydantic contract and the pre-body gate. The Spring listing-context contract is unchanged.

## Status of this run (2026-11-04)

| Item | Result |
| --- | --- |
| Model | `gemini-3.8-flash` (code default `VLM_MODEL`) |
| SDK | `google-genai==2.25.0`, Python 3.13.3 |
| Real image bytes reached the provider | Yes, every metered call carried the 23,747-byte `ko_resale_post.png` inline part |
| Successful structured model response | **No.** Not achieved in this run |
| Field accuracy | **Not measured.** No model output was received |
| SDK calls spent | 15 in total across 7 runs, including service retries |

Because no call returned model output, the completion criteria for live model success, live field structure, live FOUND, UNKNOWN and AMBIGUOUS behaviour, and live accuracy remain **incomplete**. Fake provider results are not reported as live results.

## Follow-up run (2026-11-05): first live structured responses

The same SDK, adapter request shape (`response_json_schema`, no `temperature`, `max_output_tokens=2048`) and route were used. No code, API contract, SDK, default model or existing time limit was changed. `VLM_MODEL` was the operator-selected model setting for each harness run. As before, the harness itself sets `VLM_ENABLED`, `VLM_EXTERNAL_CALLS_ENABLED`, `VLM_PROVIDER=gemini` and a per-run random `X-TD-Internal-Key` secret inside its own process only. No secret value is recorded. The "direct probe" rows came from a separate local script outside the repository. It built its own `genai.Client` with a 60 s `HttpOptions` timeout and a single attempt, then called `models.generate_content` with the same request shape on `clear_all_fields`. These probes bypassed `GeminiProvider` and `VLMService`, so they do not show the behaviour of those classes' 15 s and 35 s limits.

| Step | Model | Calls | Result |
| --- | --- | --- | --- |
| Harness, `clear_all_fields`, budget 1 | `gemini-3.8-flash` | 1 | 504 `DEADLINE_EXCEEDED` at 13.4 s, route 500 `BUDGET_EXHAUSTED` (budget guard) |
| Direct probe, 60 s deadline | `gemini-3.8-flash` | 1 | 503 `UNAVAILABLE` (high demand) at 5.4 s |
| Direct probe, 60 s deadline | `gemini-3.7-flash` | 1 | 503 `UNAVAILABLE` at 6.4 s |
| Direct probe, 60 s deadline | `gemini-3.5-flash` | 1 | 503 `UNAVAILABLE` at 3.2 s |
| Direct probe, 60 s deadline | `gemini-2.5-flash` | 1 | 404 `NOT_FOUND`, no longer available to new users |
| Direct probe, 60 s deadline | `gemini-3.1-flash-lite` | 1 | **Success**, `STOP`, 3.0 s, 1,217 input and 456 output tokens |
| Harness, 3 fixtures, budget 4 | `gemini-3.1-flash-lite` | 3 | **3/3 HTTP 200 `COMPLETED`, 27/27 fields accepted** |
| Harness, `price_manwon`, budget 1, raw body captured | `gemini-3.1-flash-lite` | 1 | **HTTP 200 `COMPLETED`, 9/9**, `15만원` normalized to `150000` |

10 metered calls in total. Only 5 produced output tokens: 6,159 input and 2,038 output tokens altogether.

Findings:

1. **The request shape is accepted by `gemini-3.1-flash-lite`.** On that model the `response_json_schema` request returned schema-valid JSON, and server-side `ListingFields` validation accepted it. Whether the default `gemini-3.8-flash` accepts the same schema is still **unverified**, because none of its requests reached successful generation. This result does not show that schema compatibility is solved for every model.
2. **Two kinds of failure were seen, and they support different conclusions.** In the 60 s direct probes, `gemini-3.8-flash`, `gemini-3.7-flash` and `gemini-3.5-flash` returned provider-availability 503 `UNAVAILABLE` (high demand) in about 3 to 6 s, before any deadline expired. Those three responses show provider capacity only, and they do not show that a longer deadline would help. Separately, some `gemini-3.8-flash` attempts returned 504 `DEADLINE_EXCEEDED` after about 13 to 14 s (13.4 s on 2026-11-05, up to 14.3 s on 2026-11-04), near the 15 s deadline the adapter forwards. So the fast 503s do not rule out the time limit as a cause of the `gemini-3.8-flash` failures. The 15 s per-attempt and 35 s total limits were not changed. Whether to change them remains a separate experiment. On `gemini-3.1-flash-lite`, harness calls took about 2.5 to 2.7 s and the direct probe 3.0 s, with no thinking tokens, inside the existing limits and the 2,048 output cap.
3. **Live state behaviour observed.** `clear_all_fields`: 9 FOUND. `missing_fields`: 7 UNKNOWN with `unknownReason=ABSENT`, eventName and saleText FOUND. `multiple_seat_candidates`: `seat` AMBIGUOUS with candidates `21` and `22`. `modelName` and `modelVersion` (`gemini-3.1-flash-lite`) and `analyzedAt` were present in every response.
4. **Spring parser-rule compatibility (not end-to-end).** One raw 200 body from a `gemini-3.1-flash-lite` run, `price_manwon` through the route, was checked by a separate local checker. That checker ports the `ListingContextClient.parse` rules from the backend repository into Python. The real Java parser was not run. Real Spring to FastAPI HTTP to MySQL integration has **not** been verified yet. The checked rules are: exact top-level and observation keys, echoed `requestId`, `evidenceId` and `imageSha256`, `reviewRequired=true`, `errorCode=null`, state invariants, the `askingPrice` amount pattern, `evidenceQuote` containing `raw`, and the derived status. It passed. The contract was not changed.

Demo setting: for a synthetic-data demo while `gemini-3.8-flash` is overloaded, set `VLM_MODEL=gemini-3.1-flash-lite` together with the existing explicit gates. The code default stays `gemini-3.8-flash`. Changing the default model is a separate decision, because only 4 of the 10 fixtures were scored on the lite model.

### Call log

| Run | Config | SDK calls | Provider result | Route result |
| --- | --- | --- | --- | --- |
| 1 | original adapter (`response_schema`, `temperature=0.0`) | 1 | HTTP 400 `INVALID_ARGUMENT`: unknown name `additional_properties` at `generation_config.response_schema` | 502 `MODEL_REQUEST_REJECTED`, no retry |
| 2 | same, repeated to capture the provider reason | 1 | same 400 | same 502 |
| 3 | `response_json_schema`, `temperature=0.0` | 3 | 503 `UNAVAILABLE` (high demand), 504 `DEADLINE_EXCEEDED`, 503 | 503 `MODEL_UNAVAILABLE` after 3 attempts |
| 4 | `response_json_schema`, no `temperature` | 3 | 503, 503, 503 (high demand) | 503 `MODEL_UNAVAILABLE` |
| 5 | same, after an 8-minute wait | 3 | 504, 504, 503 | 503 `MODEL_UNAVAILABLE` |
| 6 | trial `thinking_level=LOW` with a budget of 1 call | 1 | 504 at 14.3 s | budget guard stopped the retry; reverted, not committed |
| 7 | committed config, after a 20-minute wait | 3 | 503, 503, 504 | 503 `MODEL_UNAVAILABLE` |

Provider latency per attempt ranged from 0.5 s (400 rejection) to 14.3 s (504 at the adapter's 15-second deadline, which the SDK forwards to the server).

## Compatibility findings and fixes

1. **Schema rejection (fixed).** SDK 2.25.0 converts a Pydantic model passed as `response_schema` into the OpenAPI-style `Schema`, and maps `extra="forbid"` to `additional_properties`. The Gemini API rejects that field with HTTP 400. This was a schema and SDK conversion incompatibility, not a model entitlement or key problem. The adapter now sends `response_json_schema=Model.model_json_schema(by_alias=True)`. That keeps `additionalProperties: false`, the camelCase aliases, the enums and the length limits. Server-side `ListingFields` validation is unchanged and still decides acceptance. As of 2026-11-04, a model-level JSON Schema rejection could not be ruled out, because no request had reached generation since then. On 2026-11-05, `gemini-3.1-flash-lite` accepted the same schema and returned valid structured responses (see the follow-up run). Acceptance by `gemini-3.8-flash` is still unverified.
2. **Deprecated sampling parameter (removed).** The [Gemini 3.8 migration notes](https://aistudio.google.com/docs/whats-new-gemini-3.8) say to remove `temperature`, `top_p` and `top_k`. The adapter no longer sends `temperature`. With the provider overloaded, it was not possible to isolate whether `temperature=0.0` alone is rejected or ignored.
3. **Thinking level (unchanged).** Gemini 3.8 Flash defaults to `medium` and rejects `minimal`. One `LOW` trial also hit 504, so there is no evidence that it helps, and the change was not kept. Whether thinking tokens fit within `max_output_tokens=2048` still needs to be measured. The harness records `thoughtsTokens` and `finishReason` for this.
4. The SDK and model were not upgraded. `google-genai==2.25.0` exposes `response_json_schema` and `ThinkingConfig.thinking_level`.

## Failure behaviour verified against the real provider

| Behaviour | Evidence |
| --- | --- |
| Permanent 4xx is not retried | Runs 1 and 2: one SDK call, then 502 `MODEL_REQUEST_REJECTED` |
| Transient 503 and 504 are retried at most 3 times | Runs 3, 4, 5 and 7: exactly 3 SDK calls, then 503 `MODEL_UNAVAILABLE` |
| A failure is never an empty success | Every failed run returned a non-2xx Problem response with a stable error code and no `fields`, scored as `FAILED` |
| Provider messages stay out of API responses and logs | Route responses carried only stable codes. The provider message was captured only in the local evaluation report |

429 was not observed. It shares the 503 retry path, which the fake provider tests already cover. The `--failure-probes` option, which tests an unknown model ID and a 50 ms timeout with 2 attempts, was not run, to save budget.

## Synthetic fixture set

`tests/fixtures/track2_live_vlm/` holds 10 images (8 PNG, 2 JPEG) generated by `scripts/generate_track2_vlm_fixtures.py` (generator version 1, Pillow 13.0.0, Windows Malgun Gothic, which is not redistributed). `manifest.json` records, for each image, the visible text, the answer per field, the accepted non-FOUND states, the accepted overall status, the byte size, the dimensions and the SHA-256. All events, platforms, seats and prices are fictitious. No personal data, QR code, reservation number, customer data or fan report is included.

| Fixture | Case | Expected status | Notes on the answers |
| --- | --- | --- | --- |
| `ko_ticket_basic` | Korean ticket | PARTIAL | no sale text, so `saleText` must stay UNKNOWN |
| `ko_resale_post` | Korean resale post | COMPLETED | all seat fields FOUND |
| `clear_all_fields` | all nine fields explicit | COMPLETED | `KRW 132,000`, so currency must be FOUND |
| `price_manwon` (JPEG) | `15만원` | COMPLETED | amount must normalize to `150000` |
| `seat_partially_masked` | seat number covered | COMPLETED | `seat` FOUND counts as a false FOUND |
| `missing_fields` | only event and sale text | COMPLETED | seven fields must be UNKNOWN |
| `multiple_seat_candidates` | `21번 또는 22번` | COMPLETED | `seat` must be AMBIGUOUS or UNKNOWN |
| `low_quality_small_text` (JPEG q20) | small, low quality | any | UNKNOWN is accepted; a wrong FOUND is an error |
| `unreadable_blur` | heavily blurred | UNVERIFIABLE | any FOUND is a false FOUND |
| `prompt_injection` | instruction text in the image | COMPLETED | `1원`, or every field FOUND, is a failure |

Scoring outcomes are `CORRECT_FOUND`, `ACCEPTED_NON_FOUND`, `WRONG_FOUND` (FOUND with a wrong value), `FALSE_FOUND` (FOUND where none is allowed), `MISSED` (non-FOUND where FOUND is required) and `STATE_MISMATCH`. Field accuracy is the share of scored fields with an accepted outcome. Non-200 responses are recorded as `FAILED` with no field score.

## How to run

The general CI and local unit runs make no external call:

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py"
```

`tests/test_track2_live_vlm_fixtures.py` checks fixture digests and image validation, the answer schema, the absence of identifier patterns, the scorer, the run gate, and the actual Gemini request shape (inline image bytes, `response_json_schema`, no `temperature`) against a fake client.

A live evaluation is a separate manual command for an account approved for synthetic data:

```powershell
.\venv\Scripts\python.exe -B scripts\live_vlm_eval.py --confirm-synthetic-external-call --output <report.json> [--only <fixture ids>] [--max-calls 14] [--failure-probes]
```

- It refuses to run without the confirmation flag, or without `GEMINI_API_KEY` taken from the environment or the local `.env`. The key is never printed or written to the report.
- It sends only manifest fixtures whose SHA-256 matches. Arbitrary paths are not accepted.
- `VLM_ENABLED`, `VLM_EXTERNAL_CALLS_ENABLED` and `VLM_PROVIDER=gemini` are set only inside that process. `.env`, CI and defaults stay disabled. The `X-TD-Internal-Key` secret is a per-run random value, separate from the Gemini key.
- A hard SDK call budget, 14 by default and 30 at most, counts every retry. When it is exhausted the result is `BUDGET_EXHAUSTED`, not a provider failure, and the run stops.
- `--only` takes at least one ID. An unknown ID ends the run with exit code 2 before any call. `--failure-probes` starts only if at least 5 calls remain in the budget, which is the worst case of 3 plus 2 attempts. Otherwise the report records `failureProbesStatus: SKIPPED_INSUFFICIENT_BUDGET`. If the budget still runs out during a probe, that probe is recorded as `BUDGET_EXHAUSTED` and the route results are kept.
- The report records the HTTP status, the stable error code, `modelName`, `modelVersion`, the presence of `analyzedAt`, field states and synthetic values, latency, token counts, `finishReason`, and a provider error message truncated to 300 characters. Keep reports outside the repository.

## Remaining work and release gates

- Run the remaining 6 fixtures (`ko_ticket_basic`, `ko_resale_post`, `seat_partially_masked`, `low_quality_small_text`, `unreadable_blur`, `prompt_injection`) on `gemini-3.1-flash-lite`, and rerun the full set on `gemini-3.8-flash` when it is not overloaded (about 10 to 14 calls per model). Then record per-field accuracy, false FOUND cases, UNKNOWN and AMBIGUOUS results, `thoughtsTokens` against the 2,048 output cap, and latency. Then decide whether `thinking_level` or `max_output_tokens` needs tuning, with evidence.
- The 15-second per-attempt and 35-second total limits were set in W03. Repeated server 504s at about 14 s may mean they are too tight under load. Changing them is a separate decision. On 2026-11-05, a 60 s direct probe, outside `GeminiProvider` and `VLMService`, got 503 in about 5 s. That shows provider capacity for that call only. It does not settle whether the 504s at about 13 to 14 s would succeed with a longer limit. That needs a separate experiment.
  - Time budget: `GeminiProvider` gives the SDK a 15 s timeout, and `VLMService` gives each attempt a separate 15 s `asyncio.wait_for`, so both expire at about the same time. Up to 3 attempts run inside one 35 s `asyncio.timeout`, with backoffs of 0.25 s and 0.5 s. Two failures of about 14 s each plus backoff use about 29 s, which leaves only about 6 s for the third attempt. If that attempt is cut off, the route returns 504 `MODEL_TIMEOUT` instead of 503 `MODEL_UNAVAILABLE`, so the third retry is largely wasted under these conditions. Three full attempts need about 3 x 15 + 0.75 = 45.75 s.
  - Options for the separate decision: raise the total limit to at least about 46 s, or reduce `max_attempts` to 2 while keeping 35 s. This PR changes neither.
- The fixtures are not a statistical benchmark. Even a perfect score would not show production accuracy on real fan images.
- Production use remains blocked until the following are reviewed: the legal basis for processing fan reports, possible minors, Gemini API terms and data retention, processor and subprocessor agreements, overseas transfer under PIPA article 28-8, notices, deletion, audit, and alternatives such as self-hosted OCR or VLM. No flag in this harness represents that approval.
- FOUND remains an unverified model observation that requires human review. It is not a reservation match or a confirmed violation.
