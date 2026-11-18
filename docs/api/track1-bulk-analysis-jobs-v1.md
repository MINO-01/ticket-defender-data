# Track 1 대용량 분석 작업 API v1

`/api/v2/clusters/analyze`는 기존 소량 동기 API로 유지한다. 이 문서는 Back-end가 공개 원장 고정 뒤 호출할 별도 내부 작업 API 계약이다. 모든 경로는 `X-TD-Track1-Key`를 본문 수신 전에 요구하며, 응답은 원장 객체 key, 버킷, HMAC 비밀값을 반환하지 않는다.

## API

| Method | Path | 요청 | 응답 |
| --- | --- | --- | --- |
| POST | `/api/v2/clusters/jobs` | `Idempotency-Key`, `{manifest}` | `202 {jobId,status,existing}` |
| GET | `/api/v2/clusters/jobs/{jobId}` | 없음 | 작업 상태 |
| POST | `/api/v2/clusters/jobs/{jobId}/retry` | 없음 | `202` 재등록 상태 |
| POST | `/api/v2/clusters/jobs/{jobId}/cancel` | 없음 | `202` 취소 요청 상태 |
| GET | `/api/v2/clusters/jobs/{jobId}/results?cursor&limit&memberLimit` | 페이지 파라미터 | 완료 결과 페이지 |
| GET | `/api/v2/clusters/jobs/{jobId}/results/{clusterId}/members?cursor&limit` | member 페이지 파라미터 | 해당 군집의 모든 구성원 페이지 |
| GET | `/api/v2/clusters/jobs/{jobId}/auxiliary/shared-signal-observations?cursor&limit` | 페이지 파라미터 | 실제 공유 IP/주소 관찰 opt-in 페이지 |
| GET | `/api/v2/clusters/jobs/{jobId}/auxiliary/purchase-limit-evaluations?cursor&limit` | 페이지 파라미터 | 회차별 실제 구매 한도 평가 opt-in 페이지 |
| GET | `/api/v2/clusters/jobs/{jobId}/auxiliary/purchase-limit-evaluations/{performanceId}/findings?cursor&limit` | 페이지 파라미터 | 해당 회차의 계정별 한도 초과 후보 페이지 |

OpenAPI는 FastAPI `/openapi.json`에서 위 DTO(`Track1Bulk*`)로 생성된다. `Idempotency-Key`가 같고 manifest가 같으면 같은 `jobId`를 돌려준다. 다른 manifest면 `409 IDEMPOTENCY_CONFLICT`다. 동일 고정 입력의 실행 중/완료 중복도 기존 작업을 참조한다.

상태는 `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `INPUT_REJECTED`, `CANCELLED`, `INTERRUPTED`이다. 프로세스 재시작 중 실행 중이던 작업은 `INTERRUPTED/PROCESS_RESTARTED`로 보존되며 retry만 재시작한다. 오류는 정상 군집 0건으로 바뀌지 않는다.

## 입력 artifact manifest

`manifest`는 `artifactId`, opaque `objectKey`, `objectVersion`, `sha256`, `recordCount`, `inputFormatVersion=track1-limit-ndjson-v1`, `tenantId`, `eventId`, ledger `version/generation/revision`, `sourceVersion/sourceAsOf`, `hmacPurpose/hmacKeyVersion`, `policyVersion`, `performanceScopes`를 가진다.

입력 객체는 UTF-8 NDJSON이며 각 줄은 기존 Track 1 v2 `Track1LimitTicketInput` 한 행이다. 각 행의 `performanceId/eventId`, `sourceVersion/sourceAsOf`는 manifest 및 해당 `performanceScope`와 정확히 같아야 한다. `performanceScopes`도 동일 source metadata를 가져야 한다. Data는 configured S3-compatible bucket 및 prefix 아래의 `objectKey`만 읽고 `head_object`의 version과 manifest `objectVersion`이 같을 때만 읽는다. 다운로드하면서 SHA-256, 줄 수, 중복 ticket row, event/performance scope·출처를 검증한다. 어느 하나라도 실패하면 `INPUT_REJECTED`다.

`policyVersion`은 manifest 수준의 정책 snapshot/bundle 식별자다. 관계 그래프 분석은 `POLICY_NOT_CONFIRMED` 또는 scope별 서로 다른 confirmed policy version 때문에 차단하지 않는다. scope 결과의 `policyVersion`은 실제 CONFIRMED policy version이며 manifest bundle ID와 혼동하지 않는다. Back-end는 확정되지 않은 정책을 확정으로 승격하거나 기본 한도를 만들지 않는다.

필수 환경 변수는 `TRACK1_BULK_STATE_DB`, `TRACK1_BULK_S3_BUCKET`, `TRACK1_BULK_S3_PREFIX`, `TRACK1_BULK_S3_ENDPOINT`다. S3 credentials는 SDK 기본 credential chain에서만 읽는다. `TRACK1_BULK_MAX_PROJECTED_EDGES`(기본 5,000,000)는 투영 상한이며 초과는 `GDS_RESOURCE_EXHAUSTED` 실패다.

## 결과와 운영 제한

결과는 cluster 페이지다. 각 cluster는 `clusterId`, 조사 우선순위인 `riskScore`, `memberCount`, 제한된 `members`, `hasMoreMembers`, 실제 공유된 신호 목록을 포함한다. `nextCursor`로 다음 cluster를 읽는다. `hasMoreMembers=true`이면 cluster별 members endpoint의 cursor를 반복해 모든 구성원을 빠짐없이 읽는다. 클러스터 ID는 job 범위에서만 안정적이며 장기 동일성 식별자가 아니다.

SQLite는 account/token 관계와 결과 색인을 작업별로 영속한다. Neo4j에는 job ID가 붙은 임시 Account/Token/Signal만 배치 적재하고, **전체 관계가 구성된 뒤 단 한 번** `gds.louvain.stream`을 실행한다. chunk별 Louvain 병합은 하지 않는다. 성공·실패 모두 그 job ID의 임시 graph/node만 정리한다.

기본 `GET .../results`는 구버전 Backend #97의 소비 계약을 보존하기 위해 계속 `sharedSignalObservationsStatus: "NOT_IMPLEMENTED"`, `purchaseLimitEvaluationsStatus: "NOT_IMPLEMENTED"`와 빈 배열을 반환한다. 이는 **호환성 응답**이며 실제 계산 미구현 표기가 아니다. 빈 배열을 정상 조사 신호 0건 또는 한도 위반 0건으로 해석하면 안 된다.

실제 보조 결과는 위의 `auxiliary` opt-in 경로에서만 반환된다. 두 응답은 모두 `{jobId,status:"COMPLETED",nextCursor,...Status:"COMPLETED",...}`이며, `cursor`는 0 기반 offset이고 `limit`은 1~500이다. 완료되지 않은 job은 `409 RESULT_NOT_AVAILABLE`로 반환하며 실패·취소 job의 부분 결과를 성공 결과로 제공하지 않는다.

공유 관찰은 performance 범위에서 IP/ADDRESS별로 반환하며 raw HMAC token을 포함하지 않는다. `distinctAccountCount`는 account-token 관계의 DISTINCT 계정 수이고, 같은 계정의 반복 사용은 한 번만 센다. IP/ADDRESS 허브 임계값과 `excludedFromLouvain`은 기존 score-v2와 같다. 허브여도 관찰에는 남는다. 결제/기기 교차 값은 관찰 계정 중 실제로 해당 신호를 다른 계정과 공유한 계정 수다. `ipNetworkType`은 입력 출처가 단일 값일 때만 유지하며, token에서 추정하지 않고 나머지는 `UNKNOWN`이다. quantity provenance가 COMPLETE/NETTED/DEDUPLICATED 및 행 검증을 통과한 경우에만 `ticketQuantityStatus:"VERIFIED"`와 구매 시간 창을 제공한다. 그렇지 않으면 `UNAVAILABLE`/`null`이다. 관찰은 동일인 또는 불법 구매 확정이 아니다.

구매 한도 opt-in 평가 목록은 각 performance의 `EVALUATED`, `POLICY_NOT_CONFIRMED`, `QUANTITY_DATA_INCOMPLETE`, `POLICY_TIME_NOT_APPLICABLE`, policy version 및 `accountLimitFindingCount`를 페이지로 반환한다. `EVALUATED`의 계정별 `accountLimitFindings`와 reservation quantity는 performance별 findings endpoint에서 별도 cursor/limit 페이지로 읽는다. 따라서 대량 회차의 finding 전체를 하나의 JSON으로 적재하지 않는다. 동일 계정의 예매 수량은 합산하고 ticket reference/duplicate proof가 불완전하면 추정하거나 기본 2매 한도를 적용하지 않는다. 한도 초과는 조사 후보이지 불법 구매 확정이 아니다.

SQLite는 보조 결과를 거대한 JSON 하나가 아니라 job-scoped `bulk_shared_signal_observations`, `bulk_purchase_limit_evaluations`, `bulk_purchase_limit_findings`, `bulk_purchase_limit_reservations` 및 페이지 인덱스로 저장한다. retry는 이 테이블들을 input/cluster 상태와 같은 트랜잭션에서 모두 비운다. 결과 색인과 보조 결과 계산이 함께 실패하면 job은 `FAILED`이고 성공 0건으로 대체되지 않는다.

Backend 후속 연동에서는 #97의 기존 `collect()`를 그대로 호출하는 동안 기본 results 계약을 유지한다. 새 연동은 (1) auxiliary 두 endpoint 호출, (2) 위 상태 DTO 소비, (3) MySQL 결과 저장, (4) 관리자 조회 API 연결 순서로 별도 변경해야 한다. 그 전에는 Backend가 opt-in 응답을 소비하거나 기본 결과의 `NOT_IMPLEMENTED`를 계산 실패로 해석해서는 안 된다. 실제 S3/MinIO와 GDS 운영 검증, 5만 건 이상 성능 수치, Back-end outbox 소비/저장은 이번 구현에 포함하지 않는다.
