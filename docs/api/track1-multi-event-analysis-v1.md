# Track 1 다공연 분석 내부 계약 v1

경로: `POST /api/v2/multi-event/analyze`

이 경로는 `X-TD-Track1-Key` 내부 인증이 필요하다. 브라우저와 외부 호출자는 직접 사용하지
않는다. 기존 `/api/v1/clusters/analyze`, `/api/v2/clusters/analyze`, bulk job 경로의 동작은
변경하지 않는다.

## 입력 경계

- `analysisId`, 서버 소유 `tenantId`, 고정 `ledgerVersionSetDigest`, `analysisAsOf`, score 및
  projection version을 받는다.
- `ledgerScopes`는 두 개 이상이며 ticket의 event, performance, ledgerVersion 조합 및 선택된
  `policySnapshotId`를 고정한다.
- 모든 ticket은 하나의 supplier namespace, `TRACK1_CONNECTION` purpose, 하나의 HMAC key
  version을 사용해야 한다.
- 동일 예약 안에 여러 티켓 행이 있을 수 있으므로 Backend가 고정 scope별 `sourceRowOrdinal`을
  함께 보낸다. 이 값은 행 중복 검증에만 쓰며 계정 연결, 신호 또는 외부 식별자로 사용하지 않는다.
- input은 `extra=forbid`이며 이 JSON 경로는 ticket 최대 50,000개다. 400,000행 경로는 별도
  NDJSON artifact 기반 비동기 계약이 필요하며 이 제한을 올리는 방식으로 처리하지 않는다.
- 원본 IP, 원본 계좌, 연락처, 주소, 카드 번호는 받지 않는다.

## 후보 생성

- 안정 계정 참조와 refund account, phone, device, address의 검증된 공유 관계만 union 한다.
- payment와 IP 시간창은 공연 간 union 조건이 아니다.
- 두 회차 이상에 실제 관측된 두 계정 이상 후보만 반환한다.
- `scoreVersion`은 최종 다공연 recurrence 모델, `projectionScoreVersion`은 회차별 exp2 edge
  가중치, `projectionVersion`은 그래프 투영을 뜻한다. `algorithmVersion`은 GDS runtime만 담는다.
- Q는 고정된 policy snapshot과 동일 source version의 complete 수량이 모든 후보 계정/출처에서
  확인된 경우에만 계산한다. 그 외에는 중립값과 실제 불확실성 사유를 반환한다.
- response의 memberAccountIds와 relationships는 인증된 Spring 저장 경로 전용이다. Data는
  token 값을 response에 반환하지 않는다.

## 한계

이 경로는 제품 후보를 만들지만 Louvain GDS를 다공연 전체 그래프로 바꾸지 않는다. 회차별
Louvain은 기존 경로에서 독립적으로 유지한다. 관계 반복은 같은 강한 가명 신호가 서로 다른
공연에서 반복된 구성원 교집합으로 산출하며 추이 병합은 하지 않는다.

## 대용량 NDJSON 작업 계약

기존 50,000행 JSON 요청은 호환 경로로 유지한다. 50,000행 초과 입력은 아래 비동기
artifact 경로만 사용한다. 요청 본문에는 private object URL, bucket, 임의 경로를 넣지 않는다.
`objectKey`는 기존 `TRACK1_BULK_S3_BUCKET`과 `TRACK1_BULK_S3_PREFIX` 아래에 있는 단일
opaque 객체 이름이다.

- `POST /api/v2/multi-event/jobs`는 `{manifest: ...}`와 `Idempotency-Key`를 받는다.
- manifest는 artifact ID, object key/version, SHA-256, 400,000 이하 행 수, 입력 형식
  `multi-event-exp2-ndjson-v1`, 분석 ID/tenant, 기간, score/projection 버전, supplier namespace,
  `TRACK1_CONNECTION` purpose, HMAC key version, 고정 `ledgerScopes`를 포함한다.
- 각 `ledgerScopes` 원소는 표시용 `eventId`, digest 입력용 내부 UUID `sourceEventId`,
  `performanceId`, 고정 원장 버전, source batch와 선택적 정책 snapshot을 가진다. Data는
  Backend와 같은 Java UTF-16 길이 접두 방식으로 내부 event UUID, performance UUID, 원장
  version UUID의 canonical 집합 digest를 재계산한다.
- 각 NDJSON 행은 기존 `MultiEventTicketInput` 별칭을 사용하고 artifact 전용으로
  `sourceRowOrdinal`을 필수화한다. ordinal은 각 고정 원장 scope에서 유일해야 한다. 행의
  supplier, purpose, key version, event/performance/version은 manifest와 일치해야 한다.
- 한 줄은 1 MiB, 전체 artifact는 서버 `MULTI_EVENT_ARTIFACT_MAX_BYTES` 설정으로 제한하며
  기본값은 1 GiB다. 실제 스트림 SHA-256, S3 VersionId, 행 수와 고정 scope를 확인한 뒤에만
  그래프 처리를 시작한다.
- 요청은 `{jobId,status,existing}`을 반환한다. 상태는
  `GET /api/v2/multi-event/jobs/{jobId}`로 확인한다. 실패한 작업은 최대 3회까지
  `POST /api/v2/multi-event/jobs/{jobId}/retry`로 재시도할 수 있다. 분석 실패는 빈 성공으로
  바꾸지 않으며 resource ceiling은 실패 코드로 남긴다. 동시에 queued/running인 작업은
  최대 100개다. 한도에 닿으면 생성/재시도 요청은 HTTP 429와
  `MULTI_EVENT_JOB_QUEUE_FULL` 오류를 반환하며 Backend는 같은 artifact와 idempotency key로
  재시도한다. 단일 프로세스 startup은 stale RUNNING 작업을 `INTERRUPTED`로 표시한 다음
  남아 있는 QUEUED 작업을 다시 예약한다. 상태 조회는 recovery를 반복 실행하지 않는다.
- 회차 전체를 SQLite 페이지로 Neo4j에 단계 적재하고 회차마다 하나의 완전한 weighted GDS
  Louvain을 수행한다. chunk별 Louvain은 만들지 않는다. 전체 원장과 결과는 Python list에
  적재하지 않고 SQLite에 둔다. 작업별 상한은 입력 400,000행/1 GiB, projected edge 5,000,000,
  후보 10,000개, 후보별 구성원 50,000개, 후보별 evidence group 50,000개, recurrence 비교
  2,000,000회, 후보 구성원 행 합계 5,000,000, 후보 evidence 행 합계 5,000,000, Backend가
  관계별 구성원을 저장할 경우 그 행 합계 5,000,000이다. 집계 행 한도 오류 코드는
  `MULTI_EVENT_CANDIDATE_MEMBER_ROW_BUDGET_EXCEEDED`와
  `MULTI_EVENT_RELATIONSHIP_MEMBER_ROW_BUDGET_EXCEEDED`다. 상한 초과는 구체적인 resource
  failure code로 작업을 실패 처리하며 결과를 자르지 않는다.
- GDS 작업 동시성은 `MULTI_EVENT_MAX_CONCURRENT_JOBS`로 설정하며 기본값은 FastAPI 프로세스
  당 1개, 허용 값은 1부터 4까지다. semaphore 용량을 얻기 전에는 작업이 `QUEUED` 상태로
  남는다. 작업 저장소는 SQLite이며 admission semaphore와 stale job recovery는 프로세스
  단위다. 안전한 운영 계약은 공유 job DB를 사용하는 FastAPI process/replica 하나뿐이다.
  여러 worker/replica 동시 실행은 지원하지 않는다. 재시작 시 RUNNING 작업 복구는 단일
  서비스 프로세스 초기화 때 한 번 수행하고, 상태 조회 과정에서는 실행하지 않는다.
- 완료된 결과는 후보, 구성원, 관계 페이지 endpoint에서 숫자 offset cursor로 조회한다.
  각 페이지는 `cursor`, `nextCursor`, `totalCount`, `ledgerVersionSetDigest`, `records`를
  포함한다. page size는 1부터 500까지다. 완료 전에는 결과 조회를 차단한다.
- relation은 비밀 token을 반환하지 않는다. 각 후보는 두 서로 다른 공연에서 같은 강한
  신호가 반복된 구성원 집합의 정확한 교집합으로 만들어지며 추이 병합하지 않는다.
  관계 페이지의 `accountCount`는 그 후보의 모든 구성원이 해당 신호/출처 조합에 포함됨을
  뜻한다. 관계별 실제 token은 반환하지 않는다.

이 artifact 처리량 상한은 400,000행 입력을 받도록 설정된 계약이며 실제 400,000행 GDS
성능 검증을 의미하지 않는다.
