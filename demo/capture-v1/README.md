# 촬영용 합성 자료 capture-v1

격리 합성 환경 전용입니다. run960과 무관하고 실제 인물, 판매글, 개인정보가 없습니다. 이미지는 `scripts/generate_capture_demo.py`가 직접 그린 fixture입니다.

| 파일 | 용도 |
| --- | --- |
| `quantity_ledger.csv` | 구매 수량 분석 화면에 붙여넣는 11열 원장 67행 |
| `reservation_snapshot.csv` | 예매 자료 준비 화면에 붙여넣는 좌석 원장 67행 (5열) |
| `images/listing_A~C.png` | 팬 제보용 합성 판매글 |
| `manifest.json` | SHA-256, 반입 값, 등록할 정책, 사람이 정한 정답 |
| `environment.template.json` | 격리 환경에서 확정한 UUID 기록용 양식 |
| `vlm-results/` | 실제 Gemini 호출 기록 |

구성: R1 결제와 기기 공유 6계정, R2 간접 연결 5계정, N2 공용망 30계정, R4 조직 IP 20계정, 정상 1계정, 2매 계정, 3매 계정. 통합 사례의 예매참조는 `capture-r2-res-3`이며 좌석은 A구역 1열 9번입니다.

## 반입 전 검증

```powershell
.\venv\Scripts\python.exe -B scripts\generate_capture_demo.py --verify
```

`VERIFY_PASS`가 나와야 합니다. tenant, event, performance UUID는 파일에 없고 화면에서 격리 환경의 값을 선택합니다.

## 환경 점검 (값 출력 없음)

```powershell
.\venv\Scripts\python.exe -B scripts\check_demo_env.py --backend-env ..\ticket-defender-backend\.env
```

`RESULT: PASS`와 `INTERNAL_KEY_MATCH: MATCH`를 확인합니다.

## 실제 Gemini 분석

```powershell
.\venv\Scripts\python.exe -B scripts\run_capture_demo_vlm.py --confirm-synthetic-external-call --output demo\capture-v1\vlm-results\gemini-run.json
```

## 기록된 실제 호출 (모델 gemini-3.1-flash-lite, 2026-11-10)

| 실행 | listing_A | listing_B | listing_C |
| --- | --- | --- | --- |
| 1, 2차 (두 줄 판매 문구) | 502 `MODEL_RESPONSE_INVALID` | 502 `MODEL_RESPONSE_INVALID` | 200, 좌석 UNKNOWN |
| 3차 (A, B 판매 문구 한 줄) | 200, A/1/9 FOUND | 502 `MODEL_RESPONSE_INVALID` | 200, 좌석 UNKNOWN |

1, 2차 거부 원인은 `saleText`의 값이 인용문에 그대로 없을 때 동작하는 기존 `quote_mismatch` 검증이었습니다. 검증은 변경하지 않았습니다. 여러 줄 판매 문구는 실제 이미지에서도 같은 거부를 일으킬 수 있습니다. 3차의 listing_B 거부 원인은 추가 호출 없이 미확인으로 남겼습니다. 모델 응답은 호출마다 달라질 수 있어 촬영 전 재확인이 필요합니다.

`targetCandidateResult`는 목표입니다. 후보 대조 결과는 Backend 실행 전까지 확인되지 않았습니다.
