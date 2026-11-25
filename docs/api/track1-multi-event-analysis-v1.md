# Track 1 다공연 분석 내부 계약 v1

경로: `POST /api/v2/multi-event/analyze`

이 경로는 `X-TD-Track1-Key` 내부 인증이 필요하다. 브라우저와 외부 호출자는 직접 사용하지
않는다. 기존 `/api/v1/clusters/analyze`, `/api/v2/clusters/analyze`, bulk job 경로의 동작은
변경하지 않는다.

## 입력 경계

- `analysisId`, 서버 소유 `tenantId`, 고정 `ledgerVersionSetDigest`, `analysisAsOf`, score 및
  projection version을 받는다.
- `ledgerScopes`는 두 개 이상이며 ticket의 event, performance, ledgerVersion 조합을 완전히
  고정한다.
- 모든 ticket은 하나의 supplier namespace, `TRACK1_CONNECTION` purpose, 하나의 HMAC key
  version을 사용해야 한다.
- input은 `extra=forbid`이며 ticket 최대 50,000개다.
- 원본 IP, 원본 계좌, 연락처, 주소, 카드 번호는 받지 않는다.

## 후보 생성

- 안정 계정 참조와 refund account, phone, device, address의 검증된 공유 관계만 union 한다.
- payment와 IP 시간창은 공연 간 union 조건이 아니다.
- 두 회차 이상에 실제 관측된 두 계정 이상 후보만 반환한다.
- score-v2-exp2의 L/G/B/Q/R 결과와 수량 중립 상태를 보존한다.
- response의 memberAccountIds와 relationships는 인증된 Spring 저장 경로 전용이다. Data는
  token 값을 response에 반환하지 않는다.

## 한계

이 경로는 제품 후보를 만들지만 Louvain GDS를 다공연 전체 그래프로 바꾸지 않는다. 회차별
Louvain은 기존 경로에서 독립적으로 유지한다. Q 정책 한도는 이 초기 다공연 계약에 포함되지
않으므로 `NEUTRAL_NO_POLICY`와 이유를 반환한다.
