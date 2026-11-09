# sharedSignalObservations 계약 v1 (Backend 전달용)

상태: Data 구현 완료. Backend 저장과 조회는 미구현이다. Frontend는 같은 구조의 Zod 스키마를 사용하고, 서버가 값을 주지 않으면 미지원으로 표시한다.

`POST /api/v1/clusters/analyze`와 `POST /api/v2/clusters/analyze` 응답에 `sharedSignalObservations` 배열이 추가된다. 관찰이 없으면 `[]`다. Spring의 현재 Jackson 설정은 모르는 필드를 무시하므로 기존 호출은 깨지지 않지만, `track1-purchase-limit-analysis-v2.yaml`의 `additionalProperties: false`는 갱신이 필요하다. 계약 fixture는 `tests/fixtures/shared_signal_observations.contract.json`이며 실제 구현 출력이다.

관찰은 Louvain 군집이 아니고 부정거래, 동일인, 조직 판단이 아니다. Case 출처나 승인 근거로 자동 사용하지 않는다.

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `observationId` | string | 요청과 토큰으로 만든 불투명 ID. 다른 실행과 비교하면 안 된다 |
| `scopeType` | `EVENT` \| `PERFORMANCE` | v1은 EVENT, v2는 PERFORMANCE |
| `scopeId` | string | v1은 eventId, v2는 performanceId |
| `signalType` | `IP` \| `ADDRESS` | 결제와 기기는 관찰 대상이 아니다 |
| `distinctAccountCount` | integer | 이 신호를 공유한 구별 계정 수 |
| `scopeAccountCount` | integer | 분석 범위의 구별 계정 수 |
| `distinctReservationCount` | integer | 이 신호가 쓰인 구별 예매 수 |
| `totalTicketQuantity` | integer \| null | 수량 출처가 확인된 v2 회차만 값이 있다 |
| `ticketQuantityStatus` | `VERIFIED` \| `UNAVAILABLE` | v1과 수량 불완전 회차는 UNAVAILABLE |
| `corroboration.paymentSharedAccountCount` | integer | 공유 계정 중 같은 결제 토큰을 함께 쓴 계정 수 |
| `corroboration.deviceSharedAccountCount` | integer | 공유 계정 중 같은 기기 토큰을 함께 쓴 계정 수 |
| `ipNetworkType` | `MOBILE_CGNAT` \| `RESIDENTIAL` \| `CORPORATE_EDU` \| `DATACENTER_PROXY` \| `UNKNOWN` | 제공 메타데이터만 사용. 없음, 값 충돌, 주소 신호는 UNKNOWN |
| `purchaseWindowSeconds` | integer \| null | 수량 출처가 확인된 회차의 최초와 최종 구매 간격 |
| `excludedFromLouvain` | boolean | true면 허브로 투영에서 제외됨 |

## 임계값

- 관찰 기준: IP 9계정 이상, 주소 7계정 이상.
- Louvain 제외 기준: `max(최소 계정 수, ceil(분석 범위 구별 계정 수 x 1%))`. 관찰 기준 이상이어도 제외 기준 미만이면 `excludedFromLouvain: false`다.
- `ScoreParameters.hub_account_limits`, `hub_relative_percent`로 설정하며 `algorithmVersion`의 투영 버전은 `projection-v2.1-account-relative-hub`다. Risk Score 계산식은 변경하지 않았다(`score-v2-exp1`).

## 입력

v2 `tickets[].ipNetworkType`(선택)으로 네트워크 유형을 받는다. 값 집합은 위와 같다. IP 토큰에서 추정하지 않는다.

## Backend 반영점

1. `Track1PurchaseLimitAnalysisResponse`와 v1 응답 DTO에 필드 추가, YAML 갱신.
2. 분석 실행 결과에 저장. 토큰 원문은 응답에 없다.
3. `AnalysisRun`에 같은 이름과 구조로 `sharedSignalObservations`를 반환. Frontend는 이 필드를 읽는다.
4. `tickets[].ipNetworkType`의 출처가 정해지면 원장 입력에 추가한다. 출처가 없으면 보내지 않는다.
