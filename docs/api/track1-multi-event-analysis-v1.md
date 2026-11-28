# Track 1 다공연 분석 내부 계약 v1

경로: `POST /api/v2/multi-event/analyze`

이 경로는 `X-TD-Track1-Key` 내부 인증이 필요하다. 브라우저와 외부 호출자는 직접 사용하지
않는다. 기존 `/api/v1/clusters/analyze`, `/api/v2/clusters/analyze`, bulk job 경로의 동작은
변경하지 않는다.

## 입력 경계

- `analysisId`, 서버 소유 `tenantId`, 고정 `ledgerVersionSetDigest`, `analysisAsOf`, score 및
  projection version을 받는다.
- `ledgerScopes`는 서로 다른 performance(회차) 두 개 이상을 담아야 한다. event는 하나여도
  된다(같은 공연의 다른 회차). 회차가 하나뿐이면 `multi_event_scope_required`로 거절한다.
  각 scope는 ticket의 event, performance, ledgerVersion 조합 및 선택된 `policySnapshotId`를
  고정한다.
- 모든 ticket은 하나의 supplier namespace, `TRACK1_CONNECTION` purpose, 하나의 HMAC key
  version을 사용해야 한다.
- PR 3 선택 메타데이터 `seatZone`, `seatRow`, `seatNumber`, `reservedAt`, `paidAt`,
  `accountCreatedAt`을 활성 ticket에 포함할 수 있다. 좌석 문자열은 1~64자이고 시각은 시간대가
  있는 ISO 8601 형식이어야 한다. 이 필드들은 현재 exp2 후보 연결과 점수 계산에 쓰지 않으며,
  기존 요청에서는 생략할 수 있다.
- 취소 티켓은 `cancelledTickets` 또는 NDJSON v2의 `CANCELLED_TICKET` 레코드로 별도 전달한다.
  취소 레코드는 활성 ticket 행과 신호 입력에 합치지 않으며 exp2 구매 수량과 점수를 바꾸지 않는다.
- 동일 예약 안에 여러 티켓 행이 있을 수 있으므로 Backend가 고정 scope별 `sourceRowOrdinal`을
  함께 보낸다. 이 값은 행 중복 검증에만 쓰며 계정 연결, 신호 또는 외부 식별자로 사용하지 않는다.
- input은 `extra=forbid`이며 이 JSON 경로는 ticket 최대 50,000개다. 400,000행 경로는 별도
  NDJSON artifact 기반 비동기 계약이 필요하며 이 제한을 올리는 방식으로 처리하지 않는다.
- 원본 IP, 원본 계좌, 연락처, 주소, 카드 번호는 받지 않는다.

## 점수 버전

- `scoreVersion`은 `score-v2-exp2-multi-performance-recurrence`다. 이전 값
  `score-v2-exp2-multi-event-recurrence`는 퇴역했다. 새 JSON 요청과 새 artifact manifest에서
  이전 값을 받으면 422와 `{"detail":[{"code":"SCORE_VERSION_RETIRED"}]}`를 반환한다. 이전
  값으로 이미 저장된 artifact 작업의 상태와 결과 페이지는 읽기 전용으로 조회할 수 있고, 그런
  작업을 다시 실행하면 `INPUT_REJECTED`와 `SCORE_VERSION_RETIRED`로 끝난다.
- `projectionScoreVersion`(`score-v2-exp2`)은 회차별 exp2 edge 가중치, `projectionVersion`은
  그래프 투영을 뜻하며 바뀌지 않았다. `algorithmVersion`은 GDS runtime만 담는다.

## 후보 생성: 연결 조건

연결 조건은 "이 계정들을 하나의 후보로 묶어도 되는가"만 결정한다. 점수는 아래 점수 구성에서
따로 계산한다. 두 조건 모두 서로 다른 두 회차 쌍(같은 공연의 다른 회차 포함)에서만 성립하며,
각 회차의 전체 GDS Louvain 결과를 그대로 쓴다.

1. 강한 신호 반복(`refund_account_hash`, `phone_hash`, `device_id_hash`, `address_hash`):
   같은 token을 두 회차에서 각각 같은 군집 안의 계정들이 공유하면 두 회차 소유 계정의 정확한
   교집합(2계정 이상)이 후보 구성원이다. hard hub로 제외된 token은 근거가 아니다.
2. 같은 계정의 반복 동시 군집(`stable_account_cocluster`): 같은 계정 2개 이상이 두 회차에서
   각각 같은 군집에 있으면 그 정확한 교집합이 후보 구성원이다. 계정 ID에서 (회차, 군집)으로
   가는 역색인으로 계산하며 비교 횟수는 계정별 소속 수 `m`의 `m(m-1)/2` 합으로 미리 계산한다.
   2,000,000회를 넘으면 결과를 자르지 않고 실패한다(JSON 경로는 422, artifact 경로는 `FAILED`,
   코드 `RECURRENCE_COMPARISON_LIMIT_EXCEEDED`).

- 겹치는 교집합(A와 B의 교집합, B와 C의 교집합)은 하나로 합치지 않는다. 추이 병합은 하지 않는다.
- 결제와 IP 시간창은 서로 다른 계정을 잇는 단독 연결 조건이 아니다. 같은 계정 집합이 두 회차에서
  반복해서 같은 군집에 묶인 사실은 계정 연속성에 근거하므로 2번 조건으로 연결된다. 회차마다 다른
  계정이 같은 IP 시간창을 쓴 기숙사형 사례는 교집합이 2계정 미만이므로 후보가 아니다.
- 같은 구성원 집합이 여러 근거로 만들어지면 하나의 후보로 모으고, 관계 유형별로 근거가 관측된
  회차를 `relationships`에 남긴다. `verifiedRelationshipCount`는 (관계 유형, 회차) 쌍의 수다.
- 후보는 서로 다른 회차 2개 이상, 계정 2개 이상이어야 하고, 구성원 두 명 이상이 출처 회차에서
  직접 공유한(제외되지 않은) exp2 신호가 하나 이상 있어야 한다. 직접 공유 신호가 없으면 L이 0이어서
  설명 가능한 근거가 없으므로 후보로 내지 않는다.

## 후보 점수: 점수 구성

- L은 exp2 정의 `1 - prod(1 - r_s * dbar_s * c_s)`를 그대로 쓴다. 신호 s는 후보 구성원 두 명
  이상이 출처 회차에서 실제로 공유한 모든 exp2 신호(환불 계좌, 연락처, 기기, 주소, 결제,
  망 유형별 IP 시간창)다. 연결 조건에 쓰이지 않은 결제와 IP 시간창도 L에는 들어간다.
- `c_s`는 제외되지 않은 s token을 공유한 서로 다른 구성원 수를 후보 구성원 수로 나눈 값이다.
  `dbar_s`는 (회차, token)마다 각 회차의 `damping()` 결과를 평균한 값이다. hard hub로 제외된
  token은 L에 넣지 않고 `excludedSignals`에 신호 이름만 남긴다.
- R은 사전 등록 정의 `min((performanceCount - 1) / 3, 1)`이며 performanceCount는 후보 출처
  scope의 서로 다른 회차 수다. G, Q, B, S와 보고 band는 exp2 정의와 같다.
- `availableSignals`는 L에 들어간 신호, `unavailableSignals`는 공유되지 않은 기본 신호와 IP
  시간창 전체가 없을 때의 `ip_window`, `normalSharingPossibilities`는 공유 신호가 있을 때
  `NORMAL_SHARED_POSSIBLE`이다.
- Q는 고정된 policy snapshot과 동일 source version의 complete 수량이 모든 후보 계정/출처에서
  확인된 경우에만 계산한다. 그 외에는 중립값과 실제 불확실성 사유를 반환한다.
- response의 memberAccountIds와 relationships는 인증된 Spring 저장 경로 전용이다. Data는
  token 값을 response에 반환하지 않는다.

## 한계

이 경로는 제품 후보를 만들지만 Louvain GDS를 다회차 전체 그래프로 바꾸지 않는다. 회차별
Louvain은 기존 경로에서 독립적으로 유지한다. 서로 다른 공급자 namespace, HMAC 목적, 키 버전의
데이터는 한 요청에 섞을 수 없다. 점수는 조사 우선순위이며 위반 확정이 아니다.

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
- 각 활성 NDJSON 행은 기존 `MultiEventTicketInput` 별칭과 위 선택 메타데이터를 지원하고 artifact
  전용으로 `sourceRowOrdinal`을 필수화한다. ordinal은 각 고정 원장 scope에서 유일해야 한다. 행의
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
- relation은 비밀 token을 반환하지 않는다. 후보는 위 연결 조건(강한 신호 반복 또는
  `stable_account_cocluster`)과 점수 구성 규칙을 JSON 경로와 같은 순수 함수
  (`services/multi_event_candidates.py`)로 계산하며 추이 병합하지 않는다. 같은 입력과 군집
  결과에서 두 경로의 후보 ID, 구성원, 점수, 관계는 같아야 한다(패리티 테스트).
  관계 페이지의 `accountCount`는 그 후보의 모든 구성원이 해당 관계 유형/출처 조합에 포함됨을
  뜻한다. 관계별 실제 token은 반환하지 않는다.

이 artifact 처리량 상한은 400,000행 입력을 받도록 설정된 계약이며 실제 400,000행 GDS
성능 검증을 의미하지 않는다.
