# score-v2-exp2 사전 등록

상태: 사전등록 고정 및 구현 반영. 합성 fixture smoke는 실행했으며, 독립 holdout 평가는 미실행.

`score-v2-exp2`는 `score-v2-exp1`을 대체하지 않는 별도 실험 모델이다. 점수는 조사
우선순위이며 불법 가능성, 동일인 여부 또는 제재 결론이 아니다. 이 문서는 합성 또는
holdout 결과를 보기 전에 고정한다. 결과를 본 뒤 아래 값, 임계값, 지표를 변경하지 않는다.

## 고정 식별자와 파라미터

- score version: `score-v2-exp2`
- projection version: `projection-v2.2-exp2-ipwindow-softhub`
- IP 시간창: 회차 범위와 UTC 구매 시각 기준 600초
- 상대 허브 임계값: 범위 계정 수의 1%
- 규모 항: `0.6 + 0.4 * min(log2(n) / 5, 1)`
- 수량 중립값: `0.5`
- 반복 포화: 회차 수 4
- 보고 band: HIGH `0.40`, MEDIUM `0.20`

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

`H`가 있는 신호는 `n >= max(H, ceil(scopeAccounts * 0.01))`에서 투영하지 않는다.
`H`가 없는 환불계좌와 연락처 신호에는 hard exclusion을 적용하지 않는다. 남은 신호의
감쇠는 `n <= k`이면 1, 그 밖에는 `sqrt(k/n)`이다. 두 계정 이상이 공유한 토큰만 관계를
만든다.

## 점수와 설명

각 신호의 coverage를 `c_s`, 평균 감쇠를 `dbar_s`라 할 때 다음을 보존한다.

```text
L = 1 - product(1 - r_s * dbar_s * c_s)
G = 0.6 + 0.4 * min(log2(n) / 5, 1)
Q = mean(min(effectiveQuantity / confirmedLimit, 1)) 또는 0.5
R = min((performanceCount - 1) / 3, 1)
B = 0.5 * Q + 0.5 * R
S = round(L * G * B, 4)
```

결과는 `L`, `G`, `B`, `Q`, `R`, 가용 및 제외 신호, coverage, 수량 상태와 사유를
함께 보존한다. `Q=0.5`는 한도 정책 또는 수량 완전성이 확인됐다는 뜻이 아니다.
noisy-OR 신호 독립성도 실험 모델의 가정일 뿐이다.

## 회귀 오라클

다음 S 값은 소수 셋째 자리 허용오차 `0.001`로 고정한다.

| 사례 | S |
| --- | ---: |
| 환불계좌 20계정, 5회차 | 0.757 |
| device, 주거 IP 시간창, payment 5계정, 3회차 | 0.544 |
| 교육망 IP 시간창 22계정, 5회차 | 0.478 |
| 환불계좌 20계정, 1회차 | 0.378 |
| 교육망 IP 시간창 3계정, 5회차 | 0.363 |
| 가족 반례 3계정, 2회차 | 0.254 |
| 교육망 IP 시간창 22계정, 1회차 | 0.239 |
| 교육망 IP 시간창 3계정, 1회차 | 0.182 |
| 가족 반례 3계정, 1회차 | 0.153 |
| 기숙사 반례 40계정 | 0.108 |
| 모바일 CGNAT 반례 5계정 | 0.015 |

## 평가 경계

평가기는 분석 결과를 동결한 뒤 별도 truth 파일을 읽는다. 계정 및 군집 precision을
섞지 않으며, 군집 precision@K, 정상 반례의 최고 순위, 신호별 ablation을 출력한다.
GDS를 실행하지 못한 경우 NetworkX 대체 결과를 GDS PASS로 표기하지 않는다.

알려진 한계는 10분 창 경계, 정책 부재에서의 수량 중립값, 회차 하나일 때 `R=0`, 그리고
IP와 결제 신호만으로 회차 간 집단을 연결하지 않는다는 점이다.
