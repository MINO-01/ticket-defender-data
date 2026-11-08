# Track 1 S1~S8 및 IEEE-CIS 실효성 평가

이 평가는 기존 FastAPI API와 운영 Neo4j 데이터를 수정하지 않는 오프라인 평가 환경이다. 결과는 조사 우선순위의 비교용이며, 실제 암표 적발 정확도·불법성·사기 확률을 주장하지 않는다.

## 입력과 보안 경계

- S1~S8은 고정 seed(`20261108`), `acct-s1-*` 같은 명백한 합성 식별자, 비운영 HMAC 토큰만 사용한다. `scripts/mock_generator.py`와 `data_hasher.py`는 호출하지 않는다.
- 정답(`investigation_labels`, 실제 관계 그룹)은 `EvaluationRecord`와 분리되어 기준선 입력에 들어가지 않는다.
- S8은 tenant 범위 HMAC 토큰으로 교차 tenant 연결이 생기지 않는지 확인하는 합성 검사다. Data API 전체의 tenant 인증/인가를 증명하지 않는다.
- IEEE-CIS 원본과 `isFraud` 정답은 저장소 밖의 비공개 폴더에만 둔다. CLI는 원본을 읽기 전용으로 처리하고 원본 행·대리 속성·정답을 결과에 기록하지 않는다.
- Kaggle 다운로드, 로그인, 외부 모델 API, 유료 클라우드 리소스 생성은 이 코드에 없다.

## 기준선과 고정 정책

| 이름 | 정의 |
|---|---|
| B0 | 세 번의 고정 seed 무작위 거래 순위 (`seed`, `seed+1`, `seed+2`) |
| B1 | tenant/event 범위에서 결제·주소·기기·IP의 공유 여부를 고정 가중치로 합산 |
| B2 | 동일한 네 신호를 잇는 로컬 WCC. Louvain으로 표기하지 않음 |
| B3 | 기존 `GraphService.analyze_louvain`을 호출하는 GDS Louvain |

각 기준선은 같은 레코드·같은 순위 예산을 사용한다. 군집 점수는 구성 거래에 같은 점수를 부여하고, 여러 군집이면 최대값을 쓴다. 순위 동점은 `score DESC, ticket_id ASC`로 고정한다. 후보는 점수가 0보다 큰 거래이며, 후보 커버리지는 전체 거래 중 후보 비율이다. 미선정 거래를 정상으로 재분류하지 않는다.

`Precision@1%`과 `Recall@5%`의 K는 `floor(N * 비율)`이다. 작은 표본에서 K=0이거나 양성 레이블이 없으면 해당 지표는 `N/A`이며 0으로 바꾸지 않는다. IEEE에는 관계 군집 정답이 없으므로 ARI/NMI와 정상 공유 오탐은 `N/A`다.

## 합성 실행

기본 출력은 저장소 밖 `%USERPROFILE%\.ticket-defender\evaluation-results`이다. 다른 위치는 저장소 밖의 폴더만 지정할 수 있다.

```powershell
python -m evaluation.track1 synthetic
python -m evaluation.track1 synthetic --output-dir D:\ticket-defender-private-results
```

GDS B3는 기존 운영 Neo4j가 아니라 `tests/run_gds_integration.ps1` 방식의 격리 GDS만 명시 연결한다. URI와 비밀번호가 있어도, `tdissue19gds` 프로젝트 라벨, `neo4j` 서비스 라벨, 동적으로 매핑된 127.0.0.1 Bolt 포트, 읽기 전용 `td-issue19-gds-2.6.6.jar` 마운트가 Docker inspect에서 모두 일치해야 한다. 기본 7687 포트·원격/서비스명 URI·검증 실패는 접속 전에 `SKIPPED_GDS_ISOLATION`으로 끝나며 대체 구현을 Louvain이라고 부르지 않는다.

```powershell
$env:TD_GDS_TEST_PASSWORD = "<isolated-test-password>"
python -m evaluation.track1 synthetic --gds-uri bolt://127.0.0.1:<isolated-port>
```

## IEEE-CIS 실행 준비

사용자가 공식 원본을 직접 내려받고 대회 규칙에 동의한 후, Git 저장소 밖에 다음처럼 둔다.

```text
D:\ticket-defender-private-data\ieee-cis\train_transaction.csv
D:\ticket-defender-private-data\ieee-cis\train_identity.csv
```

기본 작은 실행은 최대 5,000개의 거래 행만 읽고 `TransactionID`로 identity를 LEFT JOIN한다. `TransactionDT` 순으로 70/15/15를 나누며 마지막 15% test에서만 최종 기준선을 측정한다. `card*`, `addr*`, `DeviceType`/`DeviceInfo`는 대리 연결 속성이고, 실제 IP가 없으므로 IP는 null이다. `isFraud`는 지표 계산에만 쓰며 토큰이나 그래프 입력에 포함하지 않는다.

```powershell
python -m evaluation.track1 ieee --data-dir D:\ticket-defender-private-data\ieee-cis --max-rows 5000
```

전체 실행은 자동으로 하지 않으며, 다음처럼 명시적으로만 허용한다.

```powershell
python -m evaluation.track1 ieee --data-dir D:\ticket-defender-private-data\ieee-cis --full
```

두 CSV가 없으면 실행은 성공 수치를 만들지 않고 `PENDING_DATASET` JSON/CSV/Markdown 집계 결과만 남긴다.

## 결과물과 논문 표

각 실행은 코드 SHA, Python/플랫폼, 실행 시각, 상태를 포함하는 집계 JSON 및 CSV와 논문 4·5장에 붙일 수 있는 Markdown 비교표를 남긴다. 열은 데이터셋·기준선·상태·Precision@1%·Recall@5%·Lift@1%·Average Precision·후보 커버리지·ARI·NMI다. 원본 행, 대리 속성, HMAC 키, 원본 정답은 결과물에 포함하지 않는다.
