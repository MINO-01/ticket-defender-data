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

OpenAPI는 FastAPI `/openapi.json`에서 위 DTO(`Track1Bulk*`)로 생성된다. `Idempotency-Key`가 같고 manifest가 같으면 같은 `jobId`를 돌려준다. 다른 manifest면 `409 IDEMPOTENCY_CONFLICT`다. 동일 고정 입력의 실행 중/완료 중복도 기존 작업을 참조한다.

상태는 `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `INPUT_REJECTED`, `CANCELLED`, `INTERRUPTED`이다. 프로세스 재시작 중 실행 중이던 작업은 `INTERRUPTED/PROCESS_RESTARTED`로 보존되며 retry만 재시작한다. 오류는 정상 군집 0건으로 바뀌지 않는다.

## 입력 artifact manifest

`manifest`는 `artifactId`, opaque `objectKey`, `objectVersion`, `sha256`, `recordCount`, `inputFormatVersion=track1-limit-ndjson-v1`, `tenantId`, `eventId`, ledger `version/generation/revision`, `sourceVersion/sourceAsOf`, `hmacPurpose/hmacKeyVersion`, `policyVersion`, `performanceScopes`를 가진다.

입력 객체는 UTF-8 NDJSON이며 각 줄은 기존 Track 1 v2 `Track1LimitTicketInput` 한 행이다. Data는 configured S3-compatible bucket 및 prefix 아래의 `objectKey`만 읽고 `head_object`의 version과 manifest `objectVersion`이 같을 때만 읽는다. 다운로드하면서 SHA-256, 줄 수, 중복 ticket row, event/performance scope를 검증한다. 어느 하나라도 실패하면 `INPUT_REJECTED`다.

필수 환경 변수는 `TRACK1_BULK_STATE_DB`, `TRACK1_BULK_S3_BUCKET`, `TRACK1_BULK_S3_PREFIX`, `TRACK1_BULK_S3_ENDPOINT`다. S3 credentials는 SDK 기본 credential chain에서만 읽는다. `TRACK1_BULK_MAX_PROJECTED_EDGES`(기본 5,000,000)는 투영 상한이며 초과는 `GDS_RESOURCE_EXHAUSTED` 실패다.

## 결과와 운영 제한

결과는 cluster 페이지다. 각 cluster는 `clusterId`, 조사 우선순위인 `riskScore`, `memberCount`, 제한된 `members`, `hasMoreMembers`, 실제 공유된 신호 목록을 포함한다. `nextCursor`로 다음 cluster를 읽는다. 클러스터 ID는 job 범위에서만 안정적이며 장기 동일성 식별자가 아니다.

SQLite는 account/token 관계와 결과 색인을 작업별로 영속한다. Neo4j에는 job ID가 붙은 임시 Account/Token/Signal만 배치 적재하고, **전체 관계가 구성된 뒤 단 한 번** `gds.louvain.stream`을 실행한다. chunk별 Louvain 병합은 하지 않는다. 성공·실패 모두 그 job ID의 임시 graph/node만 정리한다.

현재 구현은 결과 cluster 페이지와 상태/입력 검증을 제공한다. 공유 IP/주소 관찰 및 구매 한도 평가는 대량 결과 artifact 페이지로 확장하기 전에는 빈 배열로 명시된다. Back-end는 이를 “평가 없음”으로 다루고 빈 배열을 정상 판정으로 해석하면 안 된다. 실제 S3/MinIO와 GDS 운영 검증, 5만 건 이상 성능 수치, Back-end outbox 소비/저장은 이번 구현에 포함하지 않는다.
