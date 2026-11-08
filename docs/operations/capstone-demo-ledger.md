# 캡스톤 시연 합성 원장과 실제 캡처 평가 절차

악동뮤지션 대전 콘서트 (시연), 2026-12-05 18:00 KST, 대전 공연장 (시연 설정), 가상 예매처 A는 시연을 위해 가정한 설정이다. 실제 공연의 공식 일정이나 예매 원장이 아니다. 예매처 개인정보처리방침은 스키마 설계 참고 자료일 뿐이며, 어떤 업체의 데이터 제공 승인이나 API 접근 권한도 뜻하지 않는다.

## 원장 생성과 고정

생성기는 커밋된 코드에서만 고정을 허용하고, 저장소 밖의 빈 디렉터리에만 쓴다.

```powershell
.\venv\Scripts\python.exe -B -m evaluation.capstone_demo generate --output-dir <저장소 외부 경로>
.\venv\Scripts\python.exe -B -m evaluation.capstone_demo verify --ledger-dir <저장소 외부 경로>
```

`verify`는 파일 SHA-256, 행 수, sourceVersion, seed 재현 바이트, 생성기 코드 SHA를 다시 확인한다. 고정 뒤에는 좌석, 정답, 입력 토큰을 게시물 결과에 맞춰 바꾸지 않는다.

- `--performance-id`는 A3 격리 DB의 ACTIVE 공연 UUID가 정해졌을 때 쓴다. 기본값은 seed에서 유도한 UUID이며 실제 DB 등록을 뜻하지 않는다. 값을 바꿔도 `rowsSha256`과 입력 토큰은 그대로이지만 `a3_fixture.properties`의 SHA-256은 달라진다. 게시물 작성 전에 한 번만 정한다.
- `--event-ref`는 Spring 수집 범위에서 조회되는 공연 외부 참조와 같아야 한다.

## 계약과 분석 경계

- `a3_fixture.properties`는 `A3_SYNTHETIC_RESERVATION_SNAPSHOT_V1` 형식이고, `track1_ingest.json`은 현재 `AgentAnalysisRequest` 필드만 쓴다. 추가 계약 필드는 없다.
- 두 파일은 같은 `SYNTHETIC-` 예매 참조를 쓰므로 `track1_ingest.json`의 `reservationNo`를 A3 원장 reference와 그대로 대조할 수 있다.
- HMAC 토큰은 `evaluation.track1.tokens`의 비운영 평가 도메인 키로 tenant 범위와 신호 종류별로 만든다. 운영 키가 아니다.
- 생성기는 군집이나 점수를 만들지 않는다. 분석은 기존 Spring, FastAPI, Neo4j GDS Louvain 경로에서 요청별 임시 투영으로만 수행한다. 영구 Neo4j 적재를 추가하지 않는다.
- `answer_key.json`의 R1~R3, N1~N3는 주입한 관계 패턴과 평가용 정답이다. Backend나 Frontend로 전달하지 않고, 분석 결과와 분리해 보관한다.
- GraphService는 예매 단위 그래프를 쓰므로, 관계 패턴이 아닌 정상 계정의 연속 좌석 예매도 작은 커뮤니티로 나올 수 있다. 이를 없애려고 입력을 조정하지 않는다.

## 실제 캡처 평가 절차

사용자가 직접 작성하고 캡처한 PNG 또는 JPEG 4건만 대상으로 한다. 이 저장소는 게시물 이미지를 생성하지 않는다.

1. 게시물을 쓰기 전에 원장을 고정하고 manifest SHA-256을 보관한다.
2. 템플릿을 저장소 밖에 만든다: `python -B -m evaluation.capstone_demo capture-template --ledger-dir <원장> --output-dir <평가 디렉터리>`
3. A는 `demo_seats.json` 또는 고정 원장에서 일치 좌석을 고른 기능 시험이다. 탐지 정확도 실험이라고 표현하지 않는다. B, C, D는 게시글 작성 뒤 입력을 고치지 않고 결과를 확인한다.
4. 각 이미지의 SHA-256과 수집 시각을 기록한다.
5. 모델 출력을 보기 전에 사람이 이미지를 읽어 9개 필드를 `humanObservation`에 적는다. 실제 계정 정보, 연락처, 판매자 식별 정보는 적지 않는다. 모델 출력이나 원장 결과를 정답으로 재사용하지 않는다.
6. 실제 Ticket Defender 팬 제보 기능으로 업로드한다. 실제 이미지의 외부 OCR/VLM 전송은 처리 및 이전 정책이 승인되기 전에는 하지 않는다.
7. 실행된 결과만 `modelEvaluation`과 `ledgerComparison`에 그대로 적는다. 모델명, 버전, 실행 시간, 실패와 재시도 횟수, 실제 외부 SDK 호출 횟수를 포함하고, 잘못된 추출도 수정 없이 기록한다.
8. 실패는 `FAILED`와 오류 코드로 남기며 빈 정상 결과로 바꾸지 않는다.

FOUND는 사람 검토가 필요한 모델 관찰값이고, MATCH_CANDIDATE는 확정 위반이 아니다. 이 저장소에는 독립 OCR 구현이 없으므로 OCR 정확도를 주장하지 않는다. 4건은 통계적 표본이 아니다.
