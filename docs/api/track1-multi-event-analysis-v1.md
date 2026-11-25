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
