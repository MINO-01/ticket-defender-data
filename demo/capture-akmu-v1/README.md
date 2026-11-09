# 악동뮤지션 시연 합성 자료 capture-akmu-v1

격리 합성 환경 전용이며 `finalForImport: false`입니다. 최종 반입 완료 자료가 아닙니다. 실제 인물, 판매자, 연락처, 판매글이 없고 이미지는 `scripts/generate_capture_demo.py`가 직접 그린 fixture입니다. 고정 960석 원장, run960, 푸른별, Autumn Live fixture는 변경하지 않았습니다.

공연: 악동뮤지션 대전 콘서트 (합성 시연), 2026-12-05 18:00 KST, 대전 공연장 (가상), 19, 20, 21구역.

## 왜 별도 원장인가

고정 960석 원장의 19구역 8열 12번은 `SYNTHETIC-CAPSTONE-DEMO-287658`, 계정 `SYNTH-ACCT-66170`이며 정답지의 어떤 패턴에도 속하지 않아 R2와 연결되지 않습니다. 그래서 그 원장을 고치지 않고 이 자료를 새로 고정했습니다. 좌석 연결은 분석 결과를 보기 전에 정했습니다.

| 파일 | 용도 |
| --- | --- |
| `quantity_ledger.csv` | 구매 수량 분석 화면에 붙여넣는 11열 원장 67행 |
| `reservation_snapshot.csv` | 예매 자료 준비 화면에 붙여넣는 좌석 원장 67행 (5열) |
| `images/listing_A~C.png` | 팬 제보용 합성 게시물 |
| `manifest.json` | SHA-256, 반입 값, 등록할 정책, 사람이 정한 정답 |
| `environment.template.json` | 격리 환경에서 확정한 UUID 기록용 양식. 자리표시 값은 최종 식별자가 아님 |
| `vlm-results/` | 실제 Gemini 호출 기록 |

구성: R1 6계정, R2 5계정, N2 30계정, R4 20계정, 정상 1계정, 2매 계정, 3매 계정.

| 이미지 | 내용 | 목표 (결과 아님) |
| --- | --- | --- |
| listing_A | 19구역 8열 12번, 200,000원 | MATCH_CANDIDATE. 예매참조 `akmu-r2-res-3` |
| listing_B | 21구역 20열 16번, 180,000원. 원장에 없는 좌석 | NO_MATCH |
| listing_C | 좌석 가림, 190,000원 | UNKNOWN 또는 UNVERIFIABLE |

## 명령

```powershell
.\venv\Scripts\python.exe -B scripts\generate_capture_demo.py --verify
.\venv\Scripts\python.exe -B scripts\check_demo_env.py --backend-env ..\ticket-defender-backend\.env
.\venv\Scripts\python.exe -B scripts\run_capture_demo_vlm.py --confirm-synthetic-external-call --output demo\capture-akmu-v1\vlm-results\gemini-run.json
```

## 실제 Gemini 기록 (gemini-3.1-flash-lite, 2026-11-10, 합성 이미지)

| 실행 | listing_A | listing_B | listing_C |
| --- | --- | --- | --- |
| `layout-iteration-1` 1차 | 502 | 502 | 200, 좌석 UNKNOWN |
| `layout-iteration-1` 2차 | 502 | 502 | 502 |
| `gemini-run-final` (현재 이미지) | 502 | 502 | 200, 좌석 UNKNOWN |

현재 이미지의 listing_A를 직접 4회 샘플링한 결과 1회 통과(19/8/12 FOUND), 3회는 기존 `invalid_currency` 검증으로 거부됐습니다. 모델이 통화를 ISO 3자 코드가 아닌 값으로 내는 경우입니다. 검증과 프롬프트는 변경하지 않았고 응답도 수정하지 않았습니다. 500 오류와 시간 초과는 없었습니다.

후보 대조(MATCH_CANDIDATE, NO_MATCH)와 LINKED는 Backend를 실행하지 않아 실제로 검증되지 않았습니다.
