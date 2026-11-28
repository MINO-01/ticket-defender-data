# score-v3-exp3 사전 등록

상태: 개발용 합성 자료만으로 작성한 사전등록 최종 검토안. 사용자가 승인한 E1 계정 coverage 방향과 질문 2~6의 E1/E2 방향을 아래 세부 규칙으로 정리했다. 평가 자료를 열거나 사용하지 않았다. 이 문서는 사용자 최종 검토와 승인 병합, `exp3-prereg` 태그 고정 전까지 기준선으로 간주하지 않는다.

`score-v3-exp3-continuity`는 `score-v2-exp2`와 별도인 실험 모델이다. 점수는 조사 우선순위이며 불법 가능성, 동일인 여부 또는 제재 결론이 아니다. exp2 값, 회귀 오라클 및 가설 H1부터 H7까지를 변경하지 않는다.

## 고정 식별자와 파라미터

- score version: `score-v3-exp3-continuity`
- projection version: `projection-v3-exp3-continuity-ipwindow-2offset`
- 기준 점수, 가중치, 임계값: `score-v2-exp2`에서 그대로 상속
- HIGH: `0.40`, MEDIUM: `0.20`
- 평가용 데이터는 파라미터나 규칙 선택에 사용하지 않는다.

| 신호 | r | k | H |
| --- | ---: | ---: | ---: |
| refund_account_hash | 0.80 | 10 | 없음 |
| phone_hash | 0.70 | 5 | 없음 |
| device_id_hash | 0.60 | 5 | 30 |
| address_hash | 0.60 | 10 | 50 |
| payment_hash | 0.20 | 3 | 20 |
| ip_window:RESIDENTIAL | 0.50 | 30 | 100 |
| ip_window:CORPORATE_EDU | 0.50 | 30 | 100 |
| ip_window:DATACENTER_PROXY | 0.60 | 30 | 100 |
| ip_window:UNKNOWN | 0.30 | 10 | 30 |
| ip_window:MOBILE_CGNAT | 0.10 | 3 | 9 |

`H`가 있는 exp2 신호는 `n >= max(H, ceil(scopeAccounts * 0.01))`에서 투영하지 않는다. 감쇠 및 두 계정 이상이 공유한 토큰만 관계를 만드는 기존 규칙도 유지한다.

| exp3 값 | 고정값 | 개발용 근거 |
| --- | --- | --- |
| E1 재예매 간격 Δ | 90초 | 결정적 합성 경계 사례에서 90초는 일치하고 91초는 불일치했다. 합성 정상 대조의 180초와 300초는 연결되지 않았다. 예매처별 실제 지연을 관찰한 값이 아니며 실제 예매처 기본값을 뜻하지 않는다. |
| E1 단일 좌석 쌍 r | 0.30 | 설계 초안의 약한 연결 값을 유지했다. 한 좌석만 공유하는 합성 쌍에 적용했다. |
| E1 반복 좌석 쌍 r | 0.70 | 설계 초안의 반복 연결 값을 유지했다. 같은 계정 쌍이 서로 다른 좌석 키 2개 이상에서 관찰될 때 적용한다. |
| E1 반복 최소 좌석 수 | 2개 | 같은 회차와 예매처 안에서 중복 행을 제거한 서로 다른 좌석 키 수로 계산한다. |
| E2 자원 연속성의 Q 조건 | 검증된 후보별 `Q >= 0.95` | 0.05 단위로 조사한 합성 가족 반례에서 Q 0.55, 0.70, 0.80, 0.90은 기존 HIGH 미만에서 새 HIGH로 바뀌는 사례를 각각 만들었다. 이 격자에서 처음으로 새 HIGH가 없었던 값은 0.95였다. 연속된 모든 실수값의 최솟값이라고 주장하지 않는다. 이 조건은 원본의 실제 `ticket_quantity`와 적용 가능한 확인된 구매 한도를 후보 범위 전체에서 검증한 경우에만 쓴다. `perAccountLimit`은 구매 수량이 아니다. |
| E2 자원 연속성의 토큰 조건 | 서로 다른 강한 자원 유형 2개 이상 | 합성 정상 대조에서 두 유형 공유를 허용하는 규칙에 따른 HIGH 증가가 1개였고, H9의 사전 한도와 같았다. 한 유형만 공유한 가족은 Q 조건도 충족하지 않으면 반복 점수를 올리지 않는다. |
| E2 연결 범위 | 직접 연결만 | 직접 연결된 후보 사이의 서로 다른 회차만 R 계산에 추가한다. C1-C2-C3 전이는 하지 않는다. 후보를 합치지 않고 관계만 남긴다. |
| E2 사용 기간 | 기기 최대 3개월, 환불 계좌는 예매처 기준 | 기간이 지난 토큰은 연결에 쓰지 않는다. 연락처와 주소는 승인된 데이터 계약의 보유기간 안에서만 쓰며, 출처나 기간이 불명확하면 제외한다. |
| E3 IP 시간창 | 600초, 오프셋 0초 및 300초 | 고정 시각 합성 경계 사례에서 0초 창은 계정을 나누고 300초 이동 창은 같은 창에 넣었다. 어느 한 창에서든 같은 창이면 연결한다. |
| H9 정상 HIGH 증가 허용치 | exp2보다 최대 1개 | 따라하기 문서의 관리자 검토 가능 한도를 유지한다. 개발 합성 대조에서 관찰한 증가량은 1개였으며 평가 결과를 본 뒤 바꾸지 않는다. |
| 주 지표 K | 5 | exp2 모의 실험 평가 부록과 동일하다. 보조 K는 3과 10이며 기존 precision@20은 참고 지표로 유지한다. |

S1부터 S8까지의 개발 전용 합성 실행은 기존 GDS 분석 경로의 개발 검증 근거로 보존한다. E1부터 E3의 값은 실제 예매처 관측이나 팀 리허설 성능이 아니라 별도 결정적 설계 합성 사례에서 정했다. 리허설 관측 계획을 대체한 이유는 승인되지 않은 사람 참여 관측이나 평가 자료가 규칙 선택에 섞이지 않도록 하고, 고정된 경계 사례를 다시 계산할 수 있게 하기 위해서다. 이는 구현 경계 확인이지 실세계 탐지 효과 검증이 아니다. 개발 자료로 평가 결과를 대신하거나 평가 자료를 열지 않는다.

개발 전용 원본 기록은 저장소 밖 프로젝트 전용 보관소의 `gate-b-calibration.py`와 `gate-b-synthetic-calibration.json`이다. 원본 SHA-256은 각각 `C55AAD9C61952D088D4DD20E69B1AF70A8A70EAB1FBFE206A19AF3BFE217F9E9`, `3FA7AF2264C50DD39D76C477081F66D7CE273166C52BAA97DCBB7BE78347F759`이며, 실제 보관 파일과 기존 문서의 해시가 일치했다. 원본은 덮어쓰지 않았다. 2026-11-28 KST에 별도 복사본을 재실행했고 Python 3.13.3에서 2026-11-28 04:47:01.808–04:47:02.223 UTC에 종료했다. 복사본의 스크립트와 JSON 해시는 각 원본과 같았고, 재생성 JSON은 원본 JSON과 byte 단위로 같았다. 실행 메타데이터는 `replay-20261128T134701KST`에 별도 보관한다. 이 재현은 기존 결정적 설계 사례만 확인한다. 아래의 새로운 E1 coverage 공식, 약한 연결 정상 반례, E1 그래프 투영/후보 생성, 동률 및 중복 제거 규칙은 이 재생 실행에서 검증하지 않았다. 별도 S1-S8 개발 실행은 고정 seed `20261108`, GDS 2.6.6, Neo4j 5.19.0으로 끝났으며 결과 JSON SHA-256은 `C2688D7EEEE5C34AA51A902B66F3F0D8C6EC3FCC8AAD29B2566E19F02C1D012F`다. 두 결과의 목적과 실행 경로를 혼동하지 않는다.

추가로 E1 weak-link 누적 반례를 기존 보정 자료와 별도 실행했다. 스크립트와 JSON은 저장소 밖 프로젝트 전용 보관소 `D:\CapstoneDesignCode\ticket-defender-workspace\.td-exp2-private-090b628c7a8c4383b0e670b6125891a0\e1-weak-link-counterexample-20261128T142137KST\`에 두었으며 SHA-256은 각각 `6AFADC9696ED1B3000D67082F9E04BDACE50057629D58749259EE9D90C94F9B9`, `8AA1C1E58F371D0FF253A9A0C029FC6045D5AC8F2EE7DBE1FD5E292F9BD91880`이다. 실행 종료시각은 2026-11-28 14:21:37.077 KST (05:21:37.077 UTC)다. 합성 계정 12개, 고정 `L_base=0.35`, 검증된 합성 `Q=1`, R3 입력의 eligible active-backed 회차 수 4개를 가정했다. 실행은 실제 ACTIVE ticket row를 생성하지 않고 이 회차 수를 고정한 산술 확인이다. 단일 weak pair 한 개가 후보 12계정 중 두 계정에 참여하면 `c1=2/12`, `L_E1=0.05`, 총 `S3=0.3392`였다. 6개 weak pair가 12계정 전체를 덮는다고 가정한 후보에서는 `c1=12/12`, `L_E1=0.30`, 총 `S3=0.4833` (HIGH)이었다. 단, 6개가 서로 분리된 pair이고 다른 edge가 없으면 그래프 후보는 여섯 개의 2계정 후보로 나뉘며 하나의 12계정 후보가 되지 않는다. 기존 신호 등으로 이 12계정이 실제 단일 후보가 되는 조건을 따로 확인해야 한다. 이 실행은 입력 후보를 고정한 결정적 산술 확인이며 GDS, 후보 생성, 실세계 분포 또는 H9를 검증하지 않았다. 이 결과로 가중치나 허용치를 재조정하지 않았다.

## 점수와 설명

새 점수 버전은 exp2와 분리한다. 새 신호가 없는 같은 입력은 exp2 회귀 오라클과 동일해야 하며, exp2 값과 오라클은 바꾸지 않는다. `n`은 후보의 서로 다른 계정 가명 수다. E1 관계가 유효하면 취소 전용 계정도 후보의 `n`과 `G`에 포함한다. `Q`의 평균은 구매 수량과 한도가 완전하게 확인된 ACTIVE 계정만 분모로 사용하며 취소 기록과 취소 전용 계정은 분자/분모에 넣지 않는다. E2를 적용해도 `L_base`와 새 E1 기여를 덮어쓰거나 제거하지 않는다.

```text
L_base = 1 - product(1 - r_s * dbar_s * c_s)
G = 0.6 + 0.4 * min(log2(n) / 5, 1)
A_Q(C) = unique (accountHmac, PerformanceScope) pairs with >= 1 validated ACTIVE ticket in C
Q(C) = 0.5 if A_Q is empty or any required quantity/policy evidence is incomplete
       otherwise mean(min(sum(actualTicketQuantity) / confirmedLimit, 1)) over A_Q
PerformanceScope = (tenantId, sourceSystem, eventId, performanceId)
P0(C) = distinct PerformanceScope values with >= 1 validated ACTIVE ticket in C
P2(C) = union of P0(D) for directly qualified E2 neighbor candidates D; empty if P0(C) is empty
P_R3(C) = P0(C) union P2(C)
R3 = 0 if |P0(C)| = 0, else min((|P_R3(C)| - 1) / 3, 1)
B3 = 0.5 * Q + 0.5 * R3
L3 = 1 - (1 - L_base) * (1 - L_E1)
S3 = round(L3 * G * B3, 4)
```

`PerformanceScope` is the unique recurrence key inside one `analysisRunId`; it prevents equal `performanceId` strings in different events, suppliers, or tenants from colliding. `P0(C)` uses selected source scopes with at least one validated ACTIVE ticket among candidate `C`'s members. Cancellation-only rows and E1 edges never add a performance scope. `P2(C)` contains only active-backed performance scopes from a direct resource-sharing neighbor, and only if `C` itself passes the E2 qualification for that pair: at least two distinct eligible resource types are shared, or `C` has verified `Q >= 0.95`. The second endpoint is evaluated separately for its own `R3`; a one-resource match does not raise the other endpoint's recurrence merely because its neighbor has high Q. An E2 resource match must come from an eligible active member record or an approved account-level resource source; an E1 cancellation record alone cannot supply it. If `P0(C)` is empty, `R3=0` even if E1 connected the candidate across several cancelled-only performance scopes. Therefore E1 can change candidate membership, `n`, `G`, and `L_E1`, but cancel-only evidence cannot increase `R3`.

`L_base`는 exp2 신호와 E3 신호를 포함하는 기존 noisy-OR다. E3의 두 시간창은 연결 여부를 찾는 대체 창이며 같은 IP 관측을 두 번 점수화하지 않는다. E1의 기여는 아래 계정 coverage의 `L_E1` 한 항으로 만든 뒤 `L_base`와 noisy-OR로 한 번 결합한다. 같은 계정이 여러 단일 관계에 참여해도 단일 유형 coverage에서는 한 번만 센다.

`A_Q(C)` contains only unique account and performance-scope pairs with at least one validated ACTIVE ticket; cancellation-only accounts and CANCELLED/REFUNDED rows do not contribute a zero or a quantity. Sum actual quantities after source-unit interpretation and duplicate handling. A `TICKET` row contributes one ticket; an `ORDER_LINE` row contributes its validated `ticket_quantity`. A confirmed account limit must apply to that exact tenant, supplier, event, performance, ledger version and quantity source. If the selected scope is incomplete, the limit is unconfirmed, a required active pair is missing, or quantity evidence conflicts, keep the existing neutral `Q=0.5` and an unknown reason rather than treating missing data as zero or averaging only the known subset. `perAccountLimit` is never purchase quantity.

The current multi-event Spring request repeats an account/performance aggregate `effectiveTicketQuantity` on active signal rows and carries policy/source status, while Data deduplicates identical provenance observations. It does not transmit the original per-row `ticket_quantity` and `quantity_unit` evidence or a complete quantity-row manifest to the multi-event contract. PR #110 sends raw `ticket_quantity` to a separate bulk-evaluation path, while Spring stores account-level findings. Therefore the requested source-unit and whole-scope completeness checks need a small, explicitly versioned Backend-to-Data quantity evidence addition; PR 4 alone cannot implement or validate them. The additive contract should bind account HMAC, tenant, supplier, event, performance, ledger version, source/as-of, policy snapshot/version and completeness evidence, and remain opt-in for exp3. Missing data or an unsupported/incomplete contract keeps exp2 requests and Q behavior unchanged. The E2 `Q >= 0.95` path is unmet when verified candidate Q is unavailable. HMAC token values are never exposed in results or logs.

## exp3 신호와 규칙

### E1. 취소 후 재예매 연쇄

- 일치는 `(tenant, supplier, performance, seatKey)` 안에서만 찾는다. supplier는 등록된 `sourceSystem` 식별자를 사용한다. 테넌트, 예매처 또는 회차 경계를 넘는 비교는 금지한다.
- `seatKey`는 구역, 열, 번호 모두 있을 때만 만든다. 각 필드는 Unicode NFKC 정규화 후 양끝 공백만 제거하고, 대소문자와 선행 0을 보존한다. 구성 필드가 없거나 빈 값이면 제외한다. 공급자가 같은 좌석을 서로 다른 문자열로 내보내는 별칭 규칙은 계약으로 확인되기 전에는 추정하지 않는다.
- 각 예약 이력은 `reserved_at`으로 예약 이벤트를 만들고, 취소 이력은 `cancelled_at`으로 취소 이벤트를 만든다. 취소 후 다시 취소된 티켓도 예약 이벤트 시각이 있으면 재예매 후보로 볼 수 있지만 ACTIVE 수량에는 포함하지 않는다. `purchased_at`으로 `reserved_at`을 채우지 않는다. 모든 시각은 offset을 확인해 UTC 순간으로 비교한다.
- 예약 이벤트와 취소 이벤트는 같은 테넌트, 예매처, 회차, 좌석 안에서만 정렬한다. 예약 이벤트는 `(reserved_at, 불투명 reservation ticket HMAC)` 오름차순으로 처리한다. 각 예약 이벤트마다 아직 짝지어지지 않은 서로 다른 계정의 취소 중 `0 < reserved_at - cancelled_at <= 90초`인 가장 최근 취소 하나를 고른다. 같은 취소 시각이면 취소 이벤트의 불투명 ticket HMAC 오름차순으로 하나를 선택한다. 한 취소 이벤트와 한 예약 이벤트는 각각 최대 한 관계에만 사용한다. 이로써 하나의 좌석에서 겹치는 취소 여러 건이 같은 재예매에 모두 연결되지 않으며, B의 취소 뒤 C가 예약한 후속 연쇄는 별도 이벤트로 A→B, B→C 방향을 보존할 수 있다.
- 원본 중복 행은 `(tenant, sourceSystem, performance, ticketHmac, eventType, eventTime, seatKey)`로 먼저 제거한다. 같은 키의 내용이 서로 다르면 임의 선택하지 않고 해당 scope 입력을 무결성 오류로 처리한다. HMAC과 좌석 원문은 응답, 로그, 분석 추적에 기록하지 않는다.
- 유효 관계의 방향은 `취소 계정 A -> 재예매 계정 B`다. 후보 그래프 투영에는 이 관계를 방향 없는 edge로 한 번만 넣고 방향 및 두 끝점의 member-level 근거는 별도 보존한다. 같은 `tenant/sourceSystem/performance`의 같은 방향 계정 쌍에서 중복 제거한 서로 다른 `seatKey`가 1개면 단일 관계, 2개 이상이면 반복 관계다. 반복 수는 행 수나 티켓 수가 아니라 좌석 키 수다.
- E1 점수 단위는 후보 참여 계정 coverage다. `A1`은 후보 구성원 중 단일 관계에 참여하고 반복 관계에는 참여하지 않은 계정 집합이다. `A2`는 반복 관계에 한 번 이상 참여한 계정 집합이다. 한 계정이 두 유형에 참여하면 강한 반복 유형 `A2`에만 배정한다. 따라서 `A1`과 `A2`는 서로 겹치지 않는다. `n`은 E1을 포함한 후보의 서로 다른 계정 수다.

```text
c1 = |A1| / n
c2 = |A2| / n
L_E1 = 1 - (1 - 0.30 * c1) * (1 - 0.70 * c2)
L3 = 1 - (1 - L_base) * (1 - L_E1)
```

위 식은 E1 각 유형에서 `d_e=1`을 적용한다. 이는 E1에 exp2 토큰 소유자 수 감쇠를 다시 적용하지 않는다는 뜻이다. E1은 이미 회차, 좌석, 시간 조건을 통과한 계정 관계를 제한된 scope에서 만들기 때문이다. 이는 우연한 취켓팅이나 같은 가족 내 다수 예약이 없다는 뜻이 아니다. 서로 다른 계정이 같은 약한 관계 유형에 추가로 참여하면 coverage가 커지고 해당 유형의 E1 기여도 증가하지만, 같은 계정의 반복 관계 행이나 pair 수를 다시 noisy-OR 항으로 곱하지 않는다. `L_E1`과 `L_base`의 noisy-OR 결합도 계산 가정이지 실세계 신호의 독립성 증거가 아니다.

기존 쌍별 누적안은 각 unique scoped pair를 별도 noisy-OR 항으로 두어 같은 계정을 pair 수만큼 반복 가산한다. 예를 들어 `n=12` 후보에서 단일 약한 edge 여섯 개가 세 계정에만 겹쳐 참여할 때 pair atom 방식은 `1-(1-0.30*2/12)^6=0.2649`지만 계정 coverage 식은 `c1=3/12`, `L_E1=0.075`다. pair atom 방식에서는 계정 재사용 횟수에 따라 같은 유형 기여가 coverage와 무관하게 계속 커지므로 이번 정의에서는 채택하지 않는다. 기존 보정 재생은 이 신규 집계식을 검증하지 않았다.

후보 생성 시 기존 exp2 graph projection에 유효 E1 edge를 추가한다. 두 끝점 계정을 같은 분석 scope의 노드로 만들고, 방향 있는 취소 A→재예매 B 근거를 저장한 채 후보 생성 입력에는 무방향 edge 하나로 투영한다. E1 edge는 기존 자격 신호와 함께 후보 생성 입력에 제공하며 E1만으로도 연결 후보를 만들 수 있다. 취소 전용 계정은 적어도 하나의 유효 E1 edge의 끝점인 경우 후보 구성원이 될 수 있으며 후보 `n`과 `G`에 한 번 포함한다. 하나의 약한 pair edge는 두 계정 후보를 만들 수 있지만, 서로 분리된 여섯 pair edge는 다른 경로가 없으면 여섯 개의 두 계정 후보다. 여섯 pair를 12계정 후보 하나로 계산한 산술 반례는 기존 신호 등으로 12계정 후보가 이미 연결된 조건에 한정한다. 최종 후보 account set은 exp3 graph algorithm 출력 그대로이며 평가 후 수동 병합하지 않는다. GDS가 edge를 분할해 H10을 만족하지 못한 경우 결과를 실패로 보고 계정을 사후 합치지 않는다. projection, 후보 구성원, member evidence에는 원문 토큰을 담지 않는다.

취소 기록은 후보 그래프의 관계 근거로만 사용한다. ACTIVE 구매 수와 `Q` 분자/분모에서는 제외한다. Q에 포함하는 계정은 선택된 source scope에서 구매 수량이 완전하고 한도 적용이 검증된 ACTIVE 구매 자료가 있는 계정뿐이다. 취소만 있는 계정의 0개 구매량을 만들지 않는다. 유효 수량 집합이 비었거나 불완전하면 exp2 중립값 `Q=0.5`를 유지하고 이유를 표시한다.

### E2. 자원 연속성 관계

- 강한 자원은 `phone_hash`, `address_hash`, `device_id_hash`, `refund_account_hash` 네 유형뿐이다. 동일한 `(tenant, sourceSystem)` 경계 안에서 서로 다른 event의 후보 사이에 실제 공유 자원이 확인될 때만 직접 관계를 만든다. 검증된 공유 자원이 없는 후보쌍은 Q가 1.00이어도 연결하지 않는다.
- 자원 일치는 선택된 전체 분석 scope의 동일 유형과 동일 HMAC 값 사이에서만 수행한다. HMAC 키 범위가 tenant 또는 예매처마다 달라 직접 비교할 수 없는 값은 미공유로 처리한다. 실제 회차 식별은 `(tenantId, sourceSystem, eventId, performanceId)`로 고정하고 각 선택 원장 `ledgerVersionId`는 근거 출처에 보존한다. 한 분석에서 회차 수는 이 경계의 고유 tuple로 센다. 같은 tuple이 여러 후보나 응답에서 반복되어도 한 번이다. 분석 식별에는 `analysisRunId`를 포함하여 서로 다른 실행의 후보와 회차를 섞지 않는다.
- 허브 제외는 같은 tenant/sourceSystem의 선택 performance scope마다 토큰 유형/값별 distinct account owner 수를 계산한다. exp2 `H`가 있는 `device_id_hash`와 `address_hash`는 scope owner 수가 `max(H, ceil(scopeAccounts * 0.01))` 이상이면 해당 scope의 연결 근거에서 제외한다. `phone_hash`와 `refund_account_hash`는 새 절대 또는 scope 상대 cutoff를 추가하지 않고 exp2에 정의된 기준을 그대로 보존한다. E2는 `k` damping으로 토큰을 감쇠하지 않고, 제외된 hub token은 자원 공유 유형 수에도 포함하지 않는다.
- 기기 값은 관측 기준 최대 3개월만 인정한다. 환불 계좌 값은 공급자가 확인한 허용 보유기간 안에서만 인정한다. 연락처와 주소 값은 승인된 source 계약에 보유기간과 기준 시각이 명시된 경우에만 인정한다. 해당 정보가 없거나 만료되었거나 HMAC scope를 확인할 수 없으면 자원을 제외한다. 어떤 원문 값도 그래프, API 응답, 로그에 추가하지 않는다.
- 실제 공유 자원이 있는 후보쌍만 대상으로 한다. 서로 다른 허용 자원 유형을 2개 이상 공유하면 양 endpoint 모두 직접 E2 관계를 통과한다. 한 유형만 공유하면 각 endpoint 후보를 따로 판정하며, 해당 endpoint의 검증된 `Q >= 0.95`일 때 그 endpoint에만 직접 이웃 회차를 더한다. Q가 확인되지 않았거나 불완전하면 조건 미충족이다. 공유 자원이 전혀 없으면 Q가 1.00이어도 연결하지 않는다.
- 후보 집합을 union하거나 다른 후보를 통해 전이 연결하지 않는다. 후보 `C`의 `P0(C)`는 선택 범위 안에서 후보 구성원 중 하나 이상이 검증된 ACTIVE 티켓을 실제 보유한 고유 `PerformanceScope`만 담는다. `P2(C)`는 `C`와 직접 공유 자원이 있는 후보 중 `C`의 endpoint 조건을 통과한 이웃의 `P0`만 합친다. 취소 전용 E1 계정, 취소 기록만 있는 회차, E1 edge 수는 `P0`, `P2` 또는 `R3`에 넣지 않는다. 따라서 E1 관계가 여러 회차를 잇더라도 취소 전용 증거만으로 R3가 증가하지 않는다. `P_R3=P0 union P2`의 크기를 기존 exp2 식에 넣고, `P0`가 비면 R3는 0으로 둔다. 동일한 `PerformanceScope`는 한 번만 센다. E2는 `L_base` 또는 `L_E1`을 제거/대체하지 않는다.

### E3. IP 시간창 보강

- 600초 IP 창을 UTC 기준 오프셋 0초와 300초로 각각 계산한다.
- 두 계정이 어느 한 창에서 같은 IP 토큰 창에 속하면 기존 IP 신호 관계로 연결한다. 두 창의 점수를 더하지 않는다.
- `reserved_at`이 제공되면 이를 사용한다. 승인된 입력 계약으로 확인할 시각이 없으면 시각을 임의 생성하지 않고 E3 신호를 사용할 수 없는 것으로 기록한다.

### E4. 하위 후보 묶음

E4는 표시 전용이며 점수, 원본 후보 계정 집합 또는 원본 후보 순위를 변경하지 않는다. 같은 범위에서 엄격한 부분집합 후보는 가장 작은 엄격한 상위 후보에 연결한다. 일부만 겹치는 후보는 묶지 않는다. 가족 표시 점수는 구성 후보의 최댓값이며, 동점 순서는 exp2 평가 부록을 따른다.

## 회귀 오라클

exp2 회귀 오라클 11개는 새 신호가 없는 동일 입력에서 exp3에서도 동일한 점수를 내야 한다. 허용오차는 소수 셋째 자리 `0.001`이며 값과 정답을 변경하지 않는다.

| exp2 기준 사례 | S |
| --- | ---: |
| 환불계좌 20계정, 5회차 | 0.757 |
| 기기, 주거 IP 시간창, 결제 5계정, 3회차 | 0.544 |
| 교육망 IP 시간창 22계정, 5회차 | 0.478 |
| 환불계좌 20계정, 1회차 | 0.378 |
| 교육망 IP 시간창 3계정, 5회차 | 0.363 |
| 가족 반례 3계정, 2회차 | 0.254 |
| 교육망 IP 시간창 22계정, 1회차 | 0.239 |
| 교육망 IP 시간창 3계정, 1회차 | 0.182 |
| 가족 반례 3계정, 1회차 | 0.153 |
| 기숙사 반례 40계정 | 0.108 |
| 모바일 CGNAT 반례 5계정 | 0.015 |

다음은 전체 점수 계산과 E1 기여만 분리한 손계산 설계 오라클이다. 표의 E1 weak-link 정상 반례는 직전 단락에 기록한 별도 결정적 산술 실행 결과와 같다. 구현 코드를 실행해 가중치나 경곗값을 선택하지 않았다.

| 사례 | 계산 | S |
| --- | --- | ---: |
| 전체 점수: E1 단일 좌석 쌍, 계정 2개, Q=0.5, 회차 1개, 다른 연결 없음 | `L_base=0`, `c1=1`, `c2=0`, `L_E1=L3=0.30`, `G=0.68`, `R3=0`, `B3=0.25`; `0.30 * 0.68 * 0.25` | 0.0510 |
| 전체 점수: E1 반복 좌석 쌍, 계정 2개, Q=0.5, 회차 1개, 다른 연결 없음 | `L_base=0`, `c1=0`, `c2=1`, `L_E1=L3=0.70`, `G=0.68`, `R3=0`, `B3=0.25`; `0.70 * 0.68 * 0.25` | 0.1190 |
| 전체 점수: 기존 신호가 연결한 후보 12계정 중 weak pair 1개, Q=1.00, R3 적격 회차 4개 | `L_base=0.35`, `c1=2/12`, `c2=0`, `L_E1=0.05`, `L3=0.3825`, `G=0.8868`, `R3=1`, `B3=1` | 0.3392 |
| 전체 점수: 같은 후보에서 분리된 weak pair 6개가 12계정 전체에 참여, Q=1.00, R3 적격 회차 4개 | `L_base=0.35`, `c1=12/12`, `c2=0`, `L_E1=0.30`, `L3=0.545`, `G=0.8868`, `R3=1`, `B3=1`; 입력 후보가 이미 하나로 연결된다는 조건부 산술 반례다 | 0.4833 |
| 전체 점수: weak pair 6개가 후보 12계정 중 같은 3계정에만 참여, Q=1.00, R3 적격 회차 4개 | 기존 신호가 12계정 후보를 형성한다고 가정한다. `c1=3/12`, `L_E1=0.075`, `L3=0.39875`, `G=0.8868`, `R3=1`, `B3=1`; pair atom 대안이면 `L_E1=0.2649`, `S3=0.4631`이지만 확정식에서는 같은 세 계정 coverage를 반복 가산하지 않는다. | 0.3536 |
| 전체 점수: E2 가족 반례, 계정 3개, Q=0.90, 자원 유형 1개, 1회차만 있음 | Q가 0.95 미만이므로 E2를 통과하지 않아 R3는 own performance 1개만 센다. `G=0.7268`, `R3=0`, `B3=0.45`, `L3=0.60` | 0.1962 |
| 전체 점수: E2 정상 대조, 계정 3개, Q=0.90, 자원 유형 2개, 직접 이웃 포함 4회차 | 두 유형 공유 조건으로 직접 연결. `G=0.7268`, `R3=1`, `B3=0.95`, `L3=0.60` | 0.4143 |
| 전체 점수: E2 고수량 표적 사례, 계정 3개, 검증 Q=1.00, 자원 유형 1개, 직접 이웃 포함 4회차 | 실제 수량 및 한도가 검증된 경우에만 Q 경로를 충족. `G=0.7268`, `R3=1`, `B3=1.00`, `L3=0.60` | 0.4361 |

E3 경계 사례는 같은 IP의 두 예매가 600초 창 경계를 사이에 2초 간격으로 지나는 입력이다. 오프셋 0초에서는 창이 다르고, 300초 이동 창에서는 같은 창이므로 E3는 연결을 유지한다. 이 사례는 점수 오라클이 아니라 창 선택 규칙의 오라클이다.

## 평가 단위와 지표

exp2 문서 부록 A.2부터 A.5의 평가 단위, 후보와 정답 집단 매칭, 동점 정렬, 분모, 정상 집단 HIGH 계산, 조사 계정 비용 정의를 그대로 사용한다. H1부터 H7도 부록 A.6의 원래 조건과 문장을 유지한다. 주 지표는 집단 단위 `precision@5`, 보조는 `precision@3`, `precision@10`이다. 기존 후보 단위 `precision@20`은 참고값으로 보존한다. 집단 재현율, 정상 집단 HIGH 수, 정답 집단 하나당 고유 검토 계정 수도 함께 보고한다.

신규 H8부터 H12는 다음처럼 판정한다.

| 가설 | 사전 판정 규칙 |
| --- | --- |
| H8 계정 교체 집단의 공연 간 연결률 증가 | 개발 전에 고정한 합성 계정 교체 사례 중 서로 다른 공연에 걸친 정답 계정 쌍을 분모로 둔다. exp2와 exp3에서 각 쌍이 하나의 후보 또는 직접 E2 관계로 연결된 비율을 계산한다. exp3 비율이 높으면 지지, 같거나 낮으면 미지지다. 분모가 0이면 판정 불가다. |
| H9 정상 집단의 HIGH 증가 제한 | exp2 문서 부록 A.4 정의의 정상 집단 HIGH 수를 버전별로 계산한다. `exp3 - exp2 <= 1`이면 지지, 초과하면 미지지다. |
| H10 재예매 양쪽 계정의 같은 후보 포함 | 고정한 아옮 합성 사례의 취소 계정과 재예매 계정이 같은 원본 후보에 함께 포함되면 사례 성공이다. 모든 사전 지정 사례가 성공하면 지지한다. 취소 행은 활성 구매 수량에서 제외한다. |
| H11 IP 경계 집단의 분할 감소 | 다른 공유 자원이 없는 고정 IP 경계 합성 집단을 대상으로, 집단 구성원이 하나의 원본 후보 안에 모두 포함되지 않는 경우를 분할로 센다. exp3의 분할 집단 비율이 exp2보다 낮으면 지지, 같거나 높으면 미지지다. |
| H12 후보 가족 표시의 점수 불변 및 중복 감소 | E4 적용 전후 각 원본 후보의 계정 집합과 S가 같고, 가족 점수와 순위가 가족 구성 점수의 최댓값 및 고정 동점 규칙을 따르며, 표시 목록 행 수가 감소하면 지지한다. 원본 후보 순위와 exp2 점수 회귀는 별도로 보존한다. |

정답이 없는 개발 자료에서 계산된 결과는 H8부터 H12의 평가 판정으로 쓰지 않는다. 평가 정답과 대응표는 결과 동결 이후에만 채점 단계에서 접근한다. 집단 매칭이 없는 경우는 기존 exp2 부록의 N/A와 0 처리 규칙을 적용한다.

## 가설 H1부터 H7까지

기존 가설을 수정하지 않고 그대로 유지한다.

- H1: exp2에서 각 암표 역할 집단 G1, G2, G3의 최고 순위가 가족 반례 F1, F2의 최고 순위보다 높다.
- H2: 같은 공연 데이터에서 exp2의 기존 `precision@5`가 v2보다 높다. 새 집단 `precision@5`로 H2 정의를 대체하지 않는다. 비교 실행 결과가 없으면 판정 불가다.
- H3: 기존 계획의 3일차 공연 B에서 P1과 P3의 역할을 바꾸고 새 브라우저 프로필을 사용한 조건에서도 H1이 유지된다. 당일 역할 교대는 날짜와 공연 차이가 없는 별도 조건이며 H3를 대체하지 않는다. 변경안은 사전등록 승인 전까지 확정하지 않는다.
- H4: 판매 시작 한 시간 뒤 같은 집 IP에서 따로 예매한 정상 계정이 어떤 후보에도 연결되지 않는다.
- H5: LTE 기기의 IP 시간창 토큰을 다른 참가자와 공유하지 않는다.
- H6 탐색: 실습실 같은 Wi-Fi를 쓰는 정상 계정이 암표 후보에 포함되는지 그대로 보고한다.
- H7 탐색: 참가자가 자원을 분산할 때 후보 순위가 내려가는지 그대로 보고하고 한계를 기록한다.

원래 날짜, 공연, 장소, 역할 교대, 브라우저 조건 또는 필요한 입력을 충족하지 못한 가설은 유사 조건으로 대체하지 않고 판정 불가로 기록한다. 해당 조건의 합성 결과는 사람 참여 평가와 구분한다. 연구윤리 절차 확인과 필요한 승인이 끝나기 전에는 사람 참여 실험을 진행하지 않는다.

당일 역할 교대는 기존 H3와 별도의 변경안으로 제시한다. 별도 승인과 필요한 연구윤리 절차 확인 전에는 H3를 대체하지 않으며 본 실험 조건으로 확정하거나 실행하지 않는다.

## 최종 문서 검토 및 사전등록 고정

사용자는 E1 계정 coverage 방향과 질문 2~6의 제안 방향을 승인했다. 이 문서는 해당 결정을 계산 규칙으로 풀어 적은 PR #45 검토안이다. 기존 exp2 가중치, 회귀 오라클 11개, H1~H7은 기준 문서 그대로 유지하며 새 H8~H12도 유지한다. 이 변경이 원격 PR에 올라가도 사용자 문서 최종 승인 및 병합 전에는 사전등록 기준선이 아니다. `exp3-prereg`는 승인 병합 뒤 해당 커밋에만 고정한다.

최종 검토에서는 다음을 확인한다.

1. `c1=|A1|/n`, `c2=|A2|/n`의 유형 coverage와 E1 noisy-OR 공식, pair별 무제한 반복 가산을 사용하지 않는 경계
2. 90초 선행 취소와 예약의 1:1 최근접 선택, 결정적 동점 처리, 좌석 정규화, 관계 방향 근거와 무방향 그래프 투영의 구분
3. 취소 전용 계정은 유효 E1 edge가 있을 때만 후보와 `n`/`G`에 포함하고 활성 수량, Q 및 취소만으로 인한 R3 증가에서 제외하는 규칙
4. E2 네 강한 자원, 실제 공유가 없는 후보쌍 제외, 직접 관계와 비전이, 후보별 검증 Q 조건, 고유 회차 scope, 보유기간 및 기존 hub cutoff
5. 결정적 산술 예시와 그래프 후보 생성 또는 GDS/H9 검증 주장을 구분한 설명

## 평가 경계와 재현성

- 평가용 두 회차 자료 및 그 정답, 대응표를 exp3 값 선택이나 개발 검증에 사용하지 않는다.
- 각 버전의 입력, 범위, 설정, 제품 커밋과 결과를 먼저 동결하고 파일 및 정규화 결과 SHA-256을 저장한다. 결과 동결 전에는 정답을 읽지 않는다.
- 결과 동결 후에만 지정된 채점 절차에서 정답과 합성 계정 대응표를 읽는다. 원래 계정 ID, 원본 CSV, 정답 파일, 대응표와 키는 저장소 밖에 둔다.
- NetworkX 또는 연결요소 기반 proxy를 실제 GDS 결과라고 부르지 않는다. GDS 미실행 결과는 미실행 또는 proxy로 분리한다.
- 제품 전체 exp2 회귀 및 전체 서비스 E2E는 기능 구현 후 최종 통합 단계에서 기존 `exp2-frozen`과 최종 버전을 비교한다. 현재는 `DEFERRED`이며 이 문서의 설계 오라클이나 S1-S8 검증으로 대체하지 않는다.
- `exp3-prereg` 태그는 이 문서의 승인 병합 커밋이 유일하게 확정된 뒤 별도 절차에서 고정한다. 기존 태그를 덮어쓰거나 이동하지 않는다.

## 한계와 미구현 항목

90초 E1 간격은 예매처 실제 취소표 재노출 시간을 측정한 값이 아니라 개발 합성 사례에서 선택한 값이다. 실제 예매처 데이터 계약과 보유기간이 확인되지 않은 토큰은 사용하지 않는다. 결정적 합성 보정은 E1부터 E3까지의 경계와 H9의 정해진 한도를 점검했을 뿐 탐지 효과를 입증하지 않는다. PR 4 구현과 테스트가 완료되기 전에는 실제 제품 점수 계산이 검증된 것으로 기록하지 않는다.
