# Track 1 Issue #30 마감 감사

기준: Data `origin/main` = `be258dfe5040c81faf4ed5af09edc62d896215ad` (2026-11-18 확인). 대상 PR은 [#31](https://github.com/MINO-01/ticket-defender-data/pull/31) `8ff9afb`, [#32](https://github.com/MINO-01/ticket-defender-data/pull/32) `89f25ed`, [#33](https://github.com/MINO-01/ticket-defender-data/pull/33) 및 [#34](https://github.com/MINO-01/ticket-defender-data/pull/34) `be258df`이다. 이 문서는 구현/기존 검증의 감사이며, 운영 GDS·원장·외부 서비스 E2E 결과가 아니다.

| # | 완료 기준 | 상태 | 코드 및 기존 검증 근거 |
| --- | --- | --- | --- |
| 1 | 같은 계정 반복 구매가 다계정 군집이 되지 않음 | IMPLEMENTED | `aggregate_accounts`/`build_account_token_graph`; `test_single_account_multi_ticket_has_no_edges` |
| 2 | Account–Token 그래프와 실제 Louvain 경로 | IMPLEMENTED | `GraphService.analyze_louvain`의 요청별 `gds.graph.project`/`gds.louvain.stream`; `test_louvain_mapping_and_request_isolation` |
| 3 | 구별 계정 기반 공유 신호 | IMPLEMENTED | `token_account_index`가 `set[AccountKey]`를 사용; `test_same_account_tokens_are_deduplicated` |
| 4 | IP·주소 허브 처리 | IMPLEMENTED | `is_hub`/`excluded_hubs`; `test_hub_is_removed_before_projection`, 상대 임계값 테스트 |
| 5 | 정상 공유와 불확실성 보존 | IMPLEMENTED | 약한 신호 감점·N1/N2/N3 회귀, `sharedSignalObservations`가 허브를 군집과 분리 |
| 6 | 계정별 유효 구매 수량 | IMPLEMENTED | `purchase_limit_evaluations`, `_quantity_rows_are_trustworthy`; 취소/환불·출처 불충분은 평가하지 않음 |
| 7 | 2매 정상과 3매 초과 분리 | IMPLEMENTED | `test_two_accounts_with_two_each_have_no_finding`, `test_three_tickets_are_finding_without_cluster` |
| 8 | 공연별 한도 FastAPI 계약 | IMPLEMENTED | `Track1LimitAnalysisRequest`, `POST /api/v2/clusters/analyze` |
| 9 | 미확인 한도/수량을 무위반으로 오판하지 않음 | IMPLEMENTED | `POLICY_NOT_CONFIRMED`, `QUANTITY_DATA_INCOMPLETE`, `POLICY_TIME_NOT_APPLICABLE`과 회귀 테스트 |
| 10 | score-v2와 algorithmVersion 구분 | IMPLEMENTED | `PROJECTION_VERSION`/`SCORE_VERSION`, GDS 버전과 함께 응답 |
| 11 | 기존 응답 및 Spring 호환성 | PARTIAL | v1 필드와 member 식별자 검증은 `test_cluster_serialization_matches_spring_fields`; Backend `bfb820c`에는 v2 어댑터·응답 검증이 있으나 실제 배포 연동/E2E는 미실행 |
| 12 | 기존 960건 원장·SHA 보존 | IMPLEMENTED | `ledger.py` 및 `test_default_output_still_matches_the_frozen_ledger_hashes`; 과거 manifest의 생성기 해시 차이는 데이터 손상과 구분 |
| 13 | 2매 한도 원장 v2 생성 | IMPLEMENTED | `ledger_v2.py`, `limit_input_v2.py`, v2 생성·무결성·max-two 테스트 |
| 14 | 평가 정답과 분석 입력 분리 | IMPLEMENTED | `answer_key.json` 분리와 `test_answer_key_is_separate_from_inputs`/`test_no_answer_labels_in_input` |
| 15 | R1–R3, N1–N3 회귀 | IMPLEMENTED | `test_track1_scoring.py`, `test_track1_v2_demo_scenario.py`, 비교 fixture |
| 16 | score-v1/v2 공정 비교 기록 | IMPLEMENTED | `compare.py`, `test_track1_score_comparison.py`, [score-v2 운영 기록](track1-account-score-v2.md) |
| 17 | 격리 GDS와 실패 상태 구분 | IMPLEMENTED | `run_gds_integration.ps1`, `test_track1_gds_integration.py`, `SKIPPED_GDS_ISOLATION` 경로 |
| 18 | 단위/계약 테스트와 CI | PARTIAL | PR head CI는 모두 성공했으나 병합 뒤 main 전용 workflow는 없음. 이번 마감 감사에서는 전체 재실행/GDS 실행을 하지 않음 |
| 19 | 구현·검증 한계 문서화 | IMPLEMENTED | 본 문서와 [score-v2 운영 기록](track1-account-score-v2.md), [평가 문서](track1-evaluation.md), [원장 절차](capstone-demo-ledger.md) |

## CI와 이번 실행

- 과거 PR CI: `Python analysis checks`는 #31 head `a381f68` ([run 42597106208](https://github.com/MINO-01/ticket-defender-data/actions/runs/42597106208)), #32 `c099564` ([42608362255](https://github.com/MINO-01/ticket-defender-data/actions/runs/42608362255)), #33 `0ef124e` ([42617768612](https://github.com/MINO-01/ticket-defender-data/actions/runs/42617768612)), #34 `3856259` ([42667857064](https://github.com/MINO-01/ticket-defender-data/actions/runs/42667857064))에서 모두 성공했다. 이는 PR head CI이며 merge commit `be258df`의 별도 main CI는 조회되지 않았다.
- 격리 GDS의 기존 결과는 PR #31 문서가 기록한 과거 검증이다. 이번 감사에서는 Docker/GDS를 실행하지 않았다.
- 범위 변경 전 이 감사에서 venv로 Track 1·원장 관련 145개 테스트를 실행해 통과했다. 이후 변경된 범위에 따라 전체 unittest, GDS, 기존 v1 원장 재현 및 score 비교는 실행하지 않았다. v2 시험 원장은 범위 변경 전에 저장소 밖 임시 경로에서 생성·검증됐으며 `finalForImport: false`였다. 이는 A3 최종 반입 검증이 아니다.

## Data 완료와 남은 의존성

Data 자체에서 Issue #30 범위의 작고 명확한 결함은 발견하지 않아 운영 코드·기존 원장·정답은 변경하지 않았다. `ACCOUNT_LIMIT_SNAPSHOT`은 명시적으로 거절하고, typed 개별 티켓의 중복·출처·시각·상태가 불완전하면 한도 판정을 하지 않는다.

Backend 최신 main `bfb820c`에는 `FastApiAdapter.requestPurchaseLimitAnalysis`의 `/api/v2/clusters/analyze` 호출과 `X-TD-Track1-Key`, `BulkLedgerTrack1OutboxConsumer`의 10,000건 미만 제한 및 결과 저장 경로가 있다. 다만 공개 원장 경로는 현재 정책을 `POLICY_NOT_CONFIRMED`, 수량을 `INCOMPLETE`로 보낸다. 고객별 확정 정책, NETTED 수량·취소/환불 출처, v2 활성화 및 실제 저장/조회 E2E는 Backend 후속이다. Frontend는 `sharedSignalObservations`와 한도 평가의 불확실성 상태를 조사 결과와 구분해 표시·조회하는 연동이 남는다.

## A3와 품질 한계

최종 A3 원장은 확정된 ACTIVE `performanceId`와 Spring이 조회하는 `eventRef`가 있어야 새 저장소 밖 디렉터리에 고정할 수 있다. 현재 시험 v2 원장은 `finalForImport: false`이며 변경하지 않는다.

기존 기록은 score-v2가 단일 계정 후보를 제거하고 후보 수를 줄였지만, 고정 k의 Precision@30 향상을 주장하지 않는다. R2 간접 연결은 분할될 수 있고 S5 결측 신호 체인은 회수율/ARI가 낮다. IEEE-CIS의 사용자 보고 AP도 v2가 v1보다 낮았으며, 모두 이번 감사에서 재측정하지 않았다. WCC 프록시는 Louvain으로 표현하지 않는다.

## 종료 권고와 다음 작업

**종료 권고: KEEP_OPEN.** Data의 #30 구현 항목은 완료됐지만, 완료 기준 11·18의 실제 Spring 배포 연동과 병합 후 main 검증은 Data 단독 증거로 닫을 수 없다. Issue 본문의 "v2 생성 기능"은 완료됐고 "최종 A3 반입"은 별도 BLOCKED 선행 조건이다.

바로 다음 작업은 Backend #79 계열에서 확정 구매 한도·NETTED 수량 출처를 v2 요청으로 연결하고, Data v2 호출/저장/조회 E2E를 실행하는 것이다. 그 뒤에만 A3 `performanceId`/`eventRef`를 확정해 새 원장을 고정하고, 전체 계약·격리 GDS·E2E 최종 검증을 수행한다.
