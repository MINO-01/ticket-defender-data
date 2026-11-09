# Track 1 계정 단위 분석과 score-v2

상태: Data 구현 완료, 격리 GDS 실측 완료(Neo4j 5.19.0, GDS 2.6.6, 합성 데이터 한정), Backend v2 계약 `PROPOSED_FOR_CROSS_REPOSITORY_REVIEW`.

위험 점수는 조사 우선순위이며 부정거래 확률, 동일인 확정, 위반 판정이 아니다. 아래 숫자는 버전이 있는 실험 파라미터이고 출시 기준이 아니다.

## 그래프

- 버전: `projection-v2-account`. 응답 예: `gds-2.6.6;projection-v2-account;score-v2-exp1`.
- 분석 범위는 v1 요청은 `eventId`, v2 요청은 `performanceId`다. 범위가 다르면 같은 계정과 토큰이어도 연결하지 않는다.
- 범위 안의 계정 하나가 Account 노드 하나가 된다. 토큰은 신호 종류별로 별도 노드다.
- 구별 계정 2개 이상이 공유한 토큰만 투영한다. 같은 계정의 반복 사용은 공유가 아니다.
- 허브는 범위 전체의 구별 계정 수로 판정해 투영 전에 제외하고 군집 근거에서도 뺀다. IP는 8계정 초과, 주소는 6계정 초과다.
- 투영할 간선이 없으면 GDS 가용성을 확인한 뒤 `COMPLETED`, `clusters: []`를 돌려준다. GDS 장애는 오류로 유지한다.
- 탐지된 계정 군집은 요청에 실제 있는 그 계정의 예매 전부로 펼쳐 기존 예매 단위 `members` 계약을 유지한다.

## score-v2-exp1

| 항목 | 값 |
| --- | --- |
| 가중치 | 결제 0.45, 기기 0.40, 주소 0.15, IP 0.05 (합계 1.05) |
| 커버리지 | 신호별로 공유 토큰에 연결된 계정 수 / 군집 계정 수 |
| 관계 강도 | Σ(가중치 × 커버리지) / 1.05 |
| 크기 계수 | min((계정 수 - 1) / 4, 1) |
| 점수 | 관계 강도 × (0.75 + 0.25 × 크기 계수) |
| 감점 | 공유 신호가 한 종류뿐이면 × 0.8 |
| 평가용 등급 | HIGH 0.6 이상, MEDIUM 0.3 이상, LOW 0.3 미만 |

가중치 합계로 나누는 정규화 때문에 네 신호가 모두 전체 계정을 연결할 때 관계 강도가 1이 된다. IP만 공유하면 최대 약 0.04이고 주소와 IP만 공유해도 약 0.19이므로 LOW를 넘지 못한다. 등급은 평가 보고용이며 API에 노출하지 않고 제재 판단에 쓰지 않는다. 티켓 수는 점수 입력이 아니다. 평가를 본 뒤 파라미터를 바꾸면 `SCORE_VERSION`을 새로 부여한다.

R/N 생성 라벨은 분석 입력에 전달하지 않는다. N1, N2, N3의 낮은 순위는 구조적 특성(약한 신호, 허브, 단일 신호 종류)에서만 나온다.

## 수량 규칙과 v2 계약

`POST /api/v2/clusters/analyze`는 Backend `docs/api/track1-purchase-limit-analysis-v2.yaml` 커밋 `d71e11f`를 구현한다. v1 경로의 스키마는 그대로지만 분석 결과는 score-v2다. `X-TD-Track1-Key` 인증은 두 경로에 모두 적용된다. 계약 문서에 따라 Spring 소비자 구현과 fixture 검증 전에는 운영 호출을 활성화하지 않는다.

- 계정별 유효 수량은 같은 회차 예매의 `effectiveTicketQuantity` 합계다. 초과 시 `accountLimitFindings`에만 기록하고 `clusters`에 넣지 않는다.
- 여러 계정의 합계는 계산하지 않는다. 동일인 합산 규칙은 승인된 정책이 없어 구현하지 않았다.
- 내부 NOT_EVALUATED 상태는 계약에서 `POLICY_NOT_CONFIRMED`, `QUANTITY_DATA_INCOMPLETE`, `POLICY_TIME_NOT_APPLICABLE`로 표현한다. 어떤 경우에도 기본 한도를 쓰지 않는다.

- `EVALUATED`는 정책 확인, `COMPLETE`, `NETTED`, `DEDUPLICATED`, 정책 기간 충족에 더해 `quantitySourceVersion`과 `quantityAsOf`가 모두 있을 때만 나온다. 계약은 두 필드를 nullable로 두지만, 출처를 알 수 없는 수량으로 한도를 판정하지 않도록 없으면 `QUANTITY_DATA_INCOMPLETE`로 돌려준다.

### 계약 대조 결과 (Backend `451d2f1`)

- `algorithmVersion` 예시가 구현과 같은 `projection-v2-account;score-v2-exp1`로 갱신됐다.
- `COMPLETE`는 `quantitySourceVersion`, `quantityAsOf`가 있어야 한다고 명시됐고 구현과 일치한다.
- `effectiveFrom` 포함, `effectiveUntil` 제외, UTC 시각 변환 기준이 명시됐고 구현과 일치한다.
- 계약 상태는 여전히 `PROPOSED_FOR_CROSS_REPOSITORY_REVIEW`다. **Spring 소비자는 아직 연결되지 않았다.** `MacroAnalysisRequest`와 `FastApiAdapter`는 v2 필드를 보내지 않고, `purchasedAt`과 `effectiveTicketQuantity`를 공급하는 코드도 없다.
- `purchasedAt`이 `quantityAsOf`보다 늦은 경우의 처리는 계약에 없다. Data는 스냅샷이 해당 구매를 포함한다고 볼 수 없으므로 그런 티켓이 하나라도 있으면 회차 전체를 `QUANTITY_DATA_INCOMPLETE`로 돌려준다(같은 시각은 평가). 잠정 동작이며 Backend 계약 반영은 OPEN이다.
- 정책과 범위 외의 스키마 오류 코드가 `ContractError`에 없어 기존 `{"detail": [{"code": ...}]}` 형식을 유지했다.
- 같은 `eventId` 안에서 `(accountId, reservationNo, eventId)`는 응답 `members` 식별자이므로 요청 전체에서 중복을 거부한다.

### 합성 시나리오 격리 검증 (2026-11-09, Neo4j 5.19.0, GDS 2.6.6)

`tests/fixtures/track1_v2_demo_scenario.json`과 manifest는 계약 검증 전용 합성 입력이며 A3 반입 원장이 아니다(`finalForImport: false`). 실제 `gds.louvain.stream`에서 결제와 기기를 공유한 4계정만 군집이 됐고, 공용 IP 허브 10계정과 단독 계정은 군집이 아니었다. 다좌석 3매 계정만 한도 초과 신호이며, 정책 미확인 회차와 수량 불완전 회차는 각각 `POLICY_NOT_CONFIRMED`, `QUANTITY_DATA_INCOMPLETE`로 남았다. 합성 구조의 동작 확인이며 탐지 성능 수치가 아니다.

### Backend #79 입력 계약 대조 (Backend `8d8f37a`, YAML은 `eed2106`과 동일)

계약 상태는 여전히 `PROPOSED_FOR_CROSS_REPOSITORY_REVIEW`다. YAML의 `x-backend-input-contract`는 Data 변경 항목을 나열하지만 필드의 위치, 타입, 필수 여부와 응답 상태 스키마가 없다. 양쪽에서 확정된 불일치가 아니므로 Data 코드는 변경하지 않았다. 계정별 집계를 개별 티켓 DTO로 꾸며 보내지 않는다.

| 구분 | 내용 | 상태 |
| --- | --- | --- |
| 현재 호환 경로 | Spring `PurchaseLimitQuantityInputContract.assess`가 `READY_FOR_INDIVIDUAL_TICKET_V2`인 입력만 현재 v2 `tickets`로 변환 | Data 구현됨, Spring 호출 미연결 |
| 정책 의미 값 미확인 | Spring이 `policyVerificationStatus=POLICY_NOT_CONFIRMED`, `policy=null`로 전송 → `POLICY_NOT_CONFIRMED` | Data 구현됨 |
| 수량 사실 누락 | Spring이 `quantityDataStatus=INCOMPLETE` 또는 `UNKNOWN`으로 전송 → `QUANTITY_DATA_INCOMPLETE` | Data 구현됨 |
| `purchasedAt > quantityAsOf` | 회차 전체 `QUANTITY_DATA_INCOMPLETE`, 같은 시각은 평가 | Data 구현됨(`e999247`), YAML 반영 OPEN |
| `ACCOUNT_LIMIT_SNAPSHOT` | Spring은 `ACCOUNT_SNAPSHOT_NOT_SUPPORTED_BY_DATA_V2`로 분리하고 Data를 호출하지 않아야 한다 | Data 미지원, BLOCKED |

새 계약에서 확정이 필요한 항목은 다음과 같다. 확정 전에는 구현하지 않는다.

- 요청: `quantityInputType`의 위치(요청, 범위, 행 단위), 스냅샷 행의 스키마와 `tickets` 공존 여부, 개별 티켓의 `ticketStatus`, `sourceVersion`, `sourceAsOf`를 행 단위로 받을지 여부, 정책의 `limitSubject`, `applicationScope`, `salesPhaseRef`, `cancellationRefundRule`, `includesPresale` 전달 방식.
- 응답: 지원하지 않는 입력의 상태값. `QUANTITY_DATA_INCOMPLETE`로 묶을지 별도 상태를 추가할지 정해야 하며, `EVALUATED`나 빈 결과로 표현하면 안 된다.
- 상태 전이: `GROSS` 규칙과 `CANCELLED`, `REFUNDED`, `UNKNOWN` 티켓의 수량 처리, `purchaseLimitCalculationQuantity`와 `currentEffectiveTicketQuantity` 중 판정 기준, `PERSON` 주체에 필요한 고객사 승인 신원 근거.
- Backend 반영점: YAML 스키마 확정, `purchasedAt > quantityAsOf` 규칙 명시, Spring v2 어댑터와 응답 저장, 평가 불가 상태를 위반 없음으로 바꾸지 않는 처리.

### 개별 티켓 입력 처리 (Backend PR #80 `82c5899` 대응)

v2 `tickets`에 Backend YAML이 이름을 정한 선택 필드를 받는다. Backend main `5d80788`의 Spring v2 DTO도 같은 필드를 갖지만 값을 공급하는 수량 원장은 Issue #81 대기이므로 **Data 구현 완료, Spring 통합 대기**다. 필드가 없는 기존 요청의 동작은 바뀌지 않는다. 아래 규칙은 평가를 막기만 하며 finding이나 점수를 새로 만들지 않는다. 계약 확정 시 바뀔 수 있는 잠정 동작이다.

| 입력 | 결과 |
| --- | --- |
| `quantityInputType: INDIVIDUAL_TICKET`, `ticketReference`, `ticketStatus: ACTIVE`, `sourceVersion`, `sourceAsOf` 모두 일치 | 행 수량을 합산해 평가 |
| 같은 `(accountId, reservationNo, eventId, ticketReference)` 재전송 | 422 `DUPLICATE_TICKET`. 합산하지 않음 |
| 같은 예매의 여러 행에 `ticketReference`가 없음 | 기존과 같이 422 또는 `QUANTITY_DATA_INCOMPLETE` |
| `ticketReference`가 다른 예매에서 재사용, 입력 유형 혼합, 유형 없이 출처 값만 존재 | `QUANTITY_DATA_INCOMPLETE` |
| `ticketStatus`가 없거나 `UNKNOWN`, `CANCELLED`, `REFUNDED` | `QUANTITY_DATA_INCOMPLETE`. 취소와 환불의 수량 처리는 미확정이다 |
| `sourceVersion`이 없거나 회차 `quantitySourceVersion`과 다름 | `QUANTITY_DATA_INCOMPLETE` |
| `sourceAsOf`가 없거나 `quantityAsOf` 이후, `purchasedAt`이 `sourceAsOf` 이후 | `QUANTITY_DATA_INCOMPLETE` |
| `ACCOUNT_LIMIT_SNAPSHOT` 또는 집계 필드 | 422. 개별 티켓 DTO로 받지 않는다 |
| `quantityGranularity` | 422. 값 정의 전이라 받지 않는다 |

다좌석 예매의 여러 행은 응답 `members`와 `reservationQuantities`에서 예매 하나로 합친다. Louvain 투영, score-v2, v1 경로, `algorithmVersion`은 변경하지 않았다.

## 시연용 v2 분석 입력

`track1_ingest.json`은 v1 형식이며 v2에 필요한 값의 출처는 다음과 같다.

| v2 필드 | 운영 입력의 출처 | 시연 합성 입력의 출처 |
| --- | --- | --- |
| `internalEventId`, `performanceId` | Spring이 인증된 범위에서 해석 (미구현) | 호출자가 지정한 격리 DB 값, 원장 manifest의 performanceId |
| `policy`, `policyVerificationStatus` | Spring의 ACTIVE 정책 (`purchase_limit_policies`) | 격리 Spring에 실제 등록한 값을 호출자가 전달. 없으면 `POLICY_NOT_CONFIRMED` |
| `purchasedAt` | 예매처 원천 데이터 (미구현) | **원장에 없음.** 호출자가 명시한 단일 합성 시각이며 `fromLedger: false`로 기록 |
| `effectiveTicketQuantity` | 취소를 반영한 예매처 수량 (미구현) | 1행 1매 합성 전제에 따른 1 |
| `quantitySourceVersion`, `quantityAsOf` | Spring의 수량 스냅샷 (미구현) | 원장 sourceVersion, 호출자가 명시한 합성 시각 |

```powershell
python -B -m evaluation.capstone_demo limit-input-v2 --ledger-dir <확정 원장 v2> --output-dir <새 디렉터리> --internal-event-id <UUID> --synthetic-purchased-at <시각> --quantity-as-of <시각> --policy-max 2 --policy-version <등록 버전> --policy-effective-from <시각>
```

원장 `verify-v2`가 PASS이고 `finalForImport: true`일 때만 생성한다. 산출물은 `track1_limit_input_v2.json`과 입력 SHA-256, 원장 SHA-256, 합성 전제를 담은 manifest이며 원장 디렉터리 밖의 빈 디렉터리에만 쓴다. 정답 파일은 읽지 않는다.

## 격리 GDS 실행 준비

| 조건 | 상태 |
| --- | --- |
| 외부 venv | `tests/run_gds_integration.ps1 -PythonPath <venv의 python.exe>`로 사용 가능 |
| 격리 | 실행마다 무작위 `tdissue19gds*` Compose 프로젝트, 127.0.0.1 동적 포트, JAR 읽기 전용 바인드. `--pull never` |
| 정리 | `down -v`로 해당 프로젝트의 컨테이너와 익명 볼륨만 제거 |
| 무결성 | `-ExpectedJarSha256` 필수. 불일치 시 Docker 호출 전에 중단 |
| 비교 | `-ComparisonLedgerDir <고정 원장> -ComparisonOutputDir <외부 경로>`로 같은 격리 환경에서 score-v1과 score-v2 비교 |
| 호환성 | 테스트 조합은 **Neo4j 5.19.0 + GDS 2.6.6**이다. 실제 기동과 `gds.version()` 2.6.6을 확인했다. 앞서 시도한 Neo4j 5.20.0은 `LinkageError: GDS 2.6.6 is not compatible with Neo4j version: dev`로 시작 단계에서 종료됐다. [GDS 공식 README](https://github.com/neo4j/graph-data-science/blob/master/README.adoc)의 `2.6.x` 행은 패치 버전 전체의 보장이 아니었고, JAR 내부 호환 모듈은 `4.4`, `5.11`~`5.19`뿐이다. 운영 `docker-compose.yml`의 5.12.0은 변경하지 않았다 |
| JAR 출처 | [GitHub 공식 릴리스 2.6.6](https://github.com/neo4j/graph-data-science/releases/tag/2.6.6)의 `neo4j-graph-data-science-2.6.6.jar` (58,452,898바이트, 2024-05-10 게시) |
| JAR 체크섬 | GitHub API의 asset `digest`는 `null`이다. **공식 체크섬은 확인되지 않아** SHA-256은 내려받은 파일로 직접 계산해 기록한다. 이는 공식 값과의 대조가 아니라 고정값 기록이다 |
| JAR 이름 | 기존 `td-issue19-gds-2.6.6.jar`는 공식 이름이 아니어서 공식 이름으로 바꿨다. 이름은 진위를 증명하지 않으며 출처와 크기, SHA-256 기록으로 판단한다 |
| 내려받은 파일 | JAR 크기 58,452,898바이트(릴리스 표기와 일치), 로컬 계산 SHA-256 `D951B83253B8FA01895ED5466DE8C63B6DD34DD53E818745D0D5C496FB516EFA`. 공식 체크섬과 대조한 값이 아니다. 이미지 `neo4j:5.19.0`은 `sha256:e5a03c2b120da495ac6d3198849818d54e6dec6209c79b975b5b5a235b2b86e4` |
| 준비 상태 | **실행 완료.** 아래 실측 결과 참조 |

실행 절차는 다음과 같다. 다운로드는 승인 뒤에만 수행한다.

```powershell
docker pull neo4j:5.19.0
curl.exe -L -o <저장소 밖 경로>\neo4j-graph-data-science-2.6.6.jar https://github.com/neo4j/graph-data-science/releases/download/2.6.6/neo4j-graph-data-science-2.6.6.jar
(Get-Item <JAR>).Length   # 58452898과 일치해야 한다
(Get-FileHash <JAR> -Algorithm SHA256).Hash
.\tests\run_gds_integration.ps1 -JarPath <JAR> -ExpectedJarSha256 <계산값> -PythonPath <외부 venv의 python.exe> -ComparisonLedgerDir <v1 원장>,<v2 원장> -ComparisonOutputDir <저장소 밖 경로> -SyntheticOutputDir <저장소 밖 경로>
```

## 격리 GDS 실측 결과 (2026-11-08, NEO4J_GDS_LOUVAIN)

실제 `gds.louvain.stream` 결과이며 아래 WCC 프록시 표와 별개다. 원장 v1은 고정 원장(seed 20261108)이고, v2는 `finalForImport: false`인 시험 생성 원장이다. 통합 테스트 6건은 모두 통과했다.

| 입력 | 점수 | 투영 노드 / 관계 (GDS 실측, 기대값 일치) | 군집 | 단일 계정 군집 | 후보 수 (N3 제외) | CandidatePrecision (N3 제외 / 음성) | 실행 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 원장 v1 | score-v1 | 3,214 / 6,934 | 220 | 214 | 265 | 0.064 / 0.063 | 4,136 |
| 원장 v1 | score-v2 | 43 / 116 | 5 | 0 | 21 | 0.810 / 0.630 | 862 |
| 원장 v2 시험 | score-v1 | 3,411 / 6,946 | 243 | 237 | 288 | 0.059 / 0.058 | 1,958 |
| 원장 v2 시험 | score-v2 | 43 / 116 | 5 | 0 | 21 | 0.810 / 0.630 | 473 |

관계 수는 무방향 투영이라 간선의 2배다. score-v1을 먼저 실행해 JVM 준비 비용이 포함되므로 실행 시간은 두 점수를 공정하게 비교하는 값이 아니다. 모든 실행 뒤 임시 그래프는 0개 남았다.

네 행 모두 Precision@10 1.0, Recall@10 0.588, Precision@17 1.0, Recall@17 1.0, Precision@30 0.567, Recall@30 1.0이다. 이번 원장의 지표는 WCC 프록시와 같다. normalSharedTopRate@30은 score-v1이 0.000, score-v2가 0.118(N1 4계정)이다.

| 패턴 | score-v2 점수 | score-v2 순위 | score-v1 점수 (원장 v1) |
| --- | --- | --- | --- |
| R1 | 1.0 | 1~6 | 0.9143 |
| R2 | 0.6825 | 7~12 | 1.0 |
| R3 | 0.5524 | 13~17 | 0.9667 |
| N3 | 0.3429 | 18~23 | 0.85 |
| N1 | 0.1786 | 24~27 | 0.3125 |
| N2 | 미탐지(허브 제외) | 없음 | 0.1567 (원장 v2는 0.7556) |

N1이 상위 30에 들어온 것은 후보가 27개뿐이어서이며 모든 R과 N3보다 뒤이고 등급은 LOW다.

### S1~S8 실측에서 확인된 약점

S2, S6, S7, S8의 B3 결과는 두 점수의 후보 커버리지가 같고, S1, S6~S8은 ARI도 1.0으로 같다. 계정 단위 투영은 다음 시나리오에서 관계 복원이 낮아졌다.

| 시나리오 | score-v1 ARI | score-v2 ARI | 후보 커버리지 v1 / v2 |
| --- | --- | --- | --- |
| S3 간접 연결 | 1.000 | 0.492 | 0.200 / 0.200 |
| S4 복합 약한 연결 | 1.000 | 0.705 | 0.333 / 0.333 |
| S5 결측 신호 체인 | 1.000 | 0.280 | 0.190 / **0.095** |

S5는 실제 Louvain에서 대상 4건 중 2건만 후보로 남았다. 약한 신호로 이어진 희소 체인이 분할될 때 내부 공유 토큰이 없는 커뮤니티가 제거되기 때문으로 보인다. S5의 Average Precision 1.0은 점수 0인 동점을 ID 순으로 정렬한 영향을 받을 수 있어 회수 성능으로 해석하지 않는다. 이 결과를 보고 `score-v2-exp1`의 가중치와 정답은 바꾸지 않았다. 개선은 새 projection 또는 score 버전의 별도 실험으로 다룬다.

### 포스터용 재실행 (2026-11-09, 커밋 `d5f365d`)

같은 조건의 격리 GDS로 다시 실행해 군집 수, 투영 규모, 패턴별 점수와 순위, 지표가 위 실측과 같음을 확인했다. score-v2 군집은 다음과 같다. 원장 v1과 v2 시험 생성본의 계정 수와 점수는 같고 티켓 수만 다르다.

| 패턴 | 계정 | 티켓 (v1 / v2 시험) | score-v2 | 공유 토큰 (결제, 기기, 주소, IP) |
| --- | --- | --- | --- | --- |
| R1 | 6 | 14 / 12 | 1.0 | 1, 1, 1, 1 |
| R2 | 6 | 13 / 12 | 0.6825 | 3, 2, 0, 0 |
| R3 | 5 | 14 / 10 | 0.5524 | 1, 1, 1, 1 |
| N3 | 6 | 7 / 8 | 0.3429 | 1, 0, 0, 0 |
| N1 | 4 | 4 / 4 | 0.1786 | 0, 0, 1, 1 |

N2 30계정은 IP 허브 제외로 군집이 되지 않는다. 합성 주입 패턴의 시연 값이며 실제 탐지 성능이나 위반 판정이 아니다. v2 원장은 `finalForImport: false`로 최종 반입용이 아니다. v1 원장의 `verify`는 생성기 해시 차이로 FAIL이며 파일 해시와 seed 재현은 통과한다.

## 보안 점검 결과

- 그래프 이름은 요청별 UUID이고 사용자 문자열은 Cypher에 들어가지 않는다. 투영 파라미터는 정수와 실수뿐이다.
- 임시 그래프는 성공과 실패 모두 정리되고, 격리 GDS에서 영구 쓰기가 없음을 확인했다.
- v1, v2 경로 모두 본문 수신 전에 `X-TD-Track1-Key`를 검증한다.
- 범위는 `eventId`(v1) 또는 `performanceId`(v2)로 분리한다. tenant는 요청에 없으며 Spring의 범위 검증과 tenant 범위 HMAC 토큰에 의존한다.
- **미해결:** Track 1 v1, v2의 `tickets`에 개수 상한이 없고 본문 크기 제한도 없다. Spring `AgentAnalysisRequest`에도 목록 상한이 없다. 내부 인증 뒤의 경로이지만 메모리 사용을 제한하지 못하므로 상한값을 정해 보완해야 한다.

## IEEE-CIS 사용자 보고값

사용자가 로컬에서 측정해 전달한 값이다. 이 저장소 작업에서 재현하지 않았고 합성 평가와 합산하지 않는다. AP는 B0 3.57%, B1 6.26%, B2 3.90%, B3 score-v1 6.61%, B3 score-v2 4.85%로 이 일반 거래 사기 데이터에서는 score-v2가 score-v1보다 낮다. IEEE 어댑터의 계정은 거래별 가상 ID이므로 다계정 탐지 정확도를 뜻하지 않는다.

## score-v1 기준선과 비교

`evaluation/track1/legacy_score_v1.py`는 main `40a3d1b`의 티켓 단위 투영과 점수를 평가 전용으로 보존한다.

```powershell
python -B -m evaluation.capstone_demo compare --ledger-dir <원장> --output-dir <저장소 외부>
```

N3(`POLICY_AMBIGUOUS_REVIEW`)는 기본 이진 지표에서 제외하지만 N3를 음성으로 처리한 지표와 탐지 수, 점수, 순위 표를 항상 함께 출력한다.

### 지표 정의 (`capstone-compare-metrics-v2-fixed-k`)

| 지표 | 정의 |
| --- | --- |
| `precisionAtK` | 같은 모집단의 상위 k개 중 점수가 양수인 R 계정 수 / k. 모집단이 k보다 작으면 `null` |
| `recallAtK` | 상위 k개의 R 적중 수 / 전체 R 계정 수 |
| `candidatePrecision` | 점수가 양수인 전체 후보 중 R 계정 비율. 모델별 후보 수에 의존하므로 k 지표와 분리 |
| `selectedCount` | k 안 또는 전체의 실제 조사 후보 수 |
| `normalSharedTopRate` | 상위 k에 포함된 N1, N2 계정 수 / 전체 N1, N2 계정 수 (34) |
| `normalSharedShareOfSelected` | 수정 전 `normalSharedTopRate` 정의 (N1, N2 수 / 실제 선택 수) |
| `positiveOnlyPrecisionAtK` | 수정 전 `precisionAtK` 정의 (적중 수 / 실제 선택 수). 후보 수가 다르면 비교 불가 |

N3는 제외한 값(`binaryExcludingN3`)과 음성으로 취급한 값(`binaryN3AsNegative`)을 모두 낸다. 점수가 0인 계정은 k 안에 들어와도 적중으로 세지 않는다.

### 수정 후 결과 (OFFLINE_WCC_PROXY_NOT_LOUVAIN, 고정 k)

같은 원장과 정답으로 다시 평가했다. 재평가에서 Louvain 행은 여전히 `SKIPPED_GDS_ISOLATION`이다.

| 입력 | 점수 | Precision@30 (N3 제외) | Precision@30 (N3 음성) | Recall@30 | 후보 수 (N3 제외) | CandidatePrecision (N3 제외) | CandidatePrecision (N3 음성) | normalSharedTopRate@30 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 원장 v1 | score-v1 | 0.567 | 0.567 | 1.00 | 265 | 0.064 | 0.063 | 0.000 |
| 원장 v1 | score-v2 | 0.567 | 0.567 | 1.00 | 21 | 0.810 | 0.630 | 0.118 |
| 원장 v2 시험 생성 | score-v1 | 0.567 | 0.567 | 1.00 | 288 | 0.059 | 0.058 | 0.000 |
| 원장 v2 시험 생성 | score-v2 | 0.567 | 0.567 | 1.00 | 21 | 0.810 | 0.630 | 0.118 |

k=10과 k=17의 Precision과 Recall은 네 행 모두 같다(P@10 1.0, R@10 0.588, P@17 1.0, R@17 1.0). 고정 k에서는 **score-v2의 Precision@30 우위가 없다.** 수정 전 표의 0.810은 분모가 21인 값이었다. score-v2의 개선은 후보 수 축소와 단일 계정 군집 제거에서 나타난다. score-v1은 N1을 상위 30 밖에 두어 0.000이지만, 이는 단일 계정 군집이 앞자리를 차지한 결과이고 score-v2는 N1 4계정이 후보 하위로 포함된다.

### 수정 전 결과 (구 정의 보존, OFFLINE_WCC_PROXY_NOT_LOUVAIN)

아래 표의 P@30은 `positiveOnlyPrecisionAtK`에 해당하며 후보 수가 다른 점수끼리 비교할 수 없다.

Docker 데몬과 GDS JAR이 없어 Louvain 행은 `SKIPPED_GDS_ISOLATION`이다. 아래 표는 연결요소로 파티션을 대신해 투영과 점수만 비교한 값이며 **Louvain 실측 성능이 아니다.** 주입 패턴이 구조적으로 뚜렷한 합성 데이터이므로 일반화 성능으로 해석하지 않는다. 같은 점수의 순서는 계정 ID로 정한다.

| 입력 | 점수 | 군집 | 단일 계정 군집 | 독립 정상 계정 후보 | R 재현율@17 | P@30 (N3 제외) | P@30 (N3 음성) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 원장 v1 (649계정) | score-v1 | 220 | 214 | 214 | 1.00 | 0.567 | 0.567 |
| 원장 v1 (649계정) | score-v2 | 5 | 0 | 0 | 1.00 | 0.810 | 0.630 |
| 원장 v2 시험 생성 (698계정) | score-v1 | 243 | 237 | 237 | 1.00 | 0.567 | 0.567 |
| 원장 v2 시험 생성 (698계정) | score-v2 | 5 | 0 | 0 | 1.00 | 0.810 | 0.630 |

P@30은 점수가 0보다 큰 상위 계정만 분모로 쓴다. score-v2는 후보가 21개 또는 27개뿐이어서 k=30을 채우지 못한다.

N3 별도 표 (6계정 전부 탐지):

| 입력 | 점수 | N3 점수 | N3 순위 |
| --- | --- | --- | --- |
| 원장 v1 | score-v1 | 0.85 | 158~163 |
| 원장 v1 | score-v2 | 0.3429 | 18~23 |
| 원장 v2 시험 생성 | score-v1 | 0.85 | 166~171 |
| 원장 v2 시험 생성 | score-v2 | 0.3429 | 18~23 |

score-v1의 N3 순위가 뒤인 것은 단일 계정 군집 200여 개가 더 높은 점수로 앞선 결과이고, score-v2의 18~23위는 전체 후보가 적어 순위가 앞당겨진 결과다. 점수 자체는 0.85에서 0.3429로 낮아졌다. R3(0.5524)와 N3의 간격이 작으므로 실측 평가에서 재확인해야 한다.

B0, B1, B2는 기존과 같고 B3는 현재 경로(score-v2)와 `B3_GDS_LOUVAIN_LEGACY_SCORE_V1`을 별도 행으로 낸다. S1~S8은 대부분 계정당 1티켓이어서 티켓 단위와 계정 단위 정의가 사실상 같다. IEEE-CIS에는 구매 한도 정보가 없어 수량 규칙을 평가하지 않는다.
