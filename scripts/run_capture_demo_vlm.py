"""촬영용 합성 이미지 listing_A/B/C를 기존 FastAPI 경로와 실제 Gemini로 분석하고 결과를 기록합니다.

    .\\venv\\Scripts\\python.exe -B scripts\\run_capture_demo_vlm.py --confirm-synthetic-external-call --output demo\\capture-v1\\vlm-results\\gemini-run.json

manifest SHA-256이 맞는 합성 이미지만 전송합니다. 키와 요청 이미지는 결과에 기록하지 않습니다.
Fake provider를 쓰지 않으며 응답을 수정하지 않습니다. GEMINI_API_KEY가 없으면 GEMINI_KEY_NOT_CONFIGURED로 종료합니다.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
from generate_capture_demo import MANIFEST, OUTPUT, verify  # noqa: E402
from live_vlm_eval import CONFIRM_FLAG, CallMeter  # noqa: E402

IMAGES = ("listing_A", "listing_B", "listing_C")
ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="합성 listing 이미지 실제 Gemini 분석")
    parser.add_argument(CONFIRM_FLAG, dest="confirmed", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=6)
    args = parser.parse_args(argv)
    if not args.confirmed:
        print(f"{CONFIRM_FLAG} 없이는 외부 호출을 하지 않습니다.", file=sys.stderr)
        return 2
    if not 1 <= args.max_calls <= 12:
        print("--max-calls 는 1 이상 12 이하만 허용합니다.", file=sys.stderr)
        return 2
    values = dotenv_values(ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY") or values.get("GEMINI_API_KEY") or ""
    if not api_key:
        print("GEMINI_KEY_NOT_CONFIGURED")
        return 3
    problems = verify()
    if problems:
        print("\n".join(problems))
        return 4

    model = os.getenv("VLM_MODEL") or values.get("VLM_MODEL") or ""
    # 이 프로세스 안에서만 쓰는 임시 내부 키로 기존 인증 게이트를 그대로 통과합니다.
    internal_key = secrets.token_urlsafe(48)
    os.environ.update({
        "VLM_ENABLED": "true", "VLM_EXTERNAL_CALLS_ENABLED": "true", "VLM_PROVIDER": "gemini", "VLM_MODEL": model,
        "VLM_INTERNAL_SHARED_SECRET": internal_key, "GEMINI_API_KEY": api_key, "VLM_MAX_CONCURRENT": "1",
    })
    meter = CallMeter(args.max_calls)
    meter.install()
    from fastapi.testclient import TestClient
    from main import app

    manifest = json.loads((OUTPUT / MANIFEST).read_text(encoding="utf-8"))
    results = []
    with TestClient(app) as client:
        for name in IMAGES:
            data = (OUTPUT / "images" / f"{name}.png").read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            started = time.perf_counter()
            response = client.post(
                "/api/v1/parse/listing-context",
                json={"requestId": str(uuid.uuid4()), "evidenceId": str(uuid.uuid4()), "imageSha256": digest, "mimeType": "image/png", "imageBase64": base64.b64encode(data).decode("ascii")},
                headers={"X-TD-Internal-Key": internal_key},
            )
            body = response.json()
            # 응답을 고치지 않습니다. 200은 서버의 응답 스키마 검증을 통과했다는 뜻입니다.
            results.append({
                "image": name, "imageSha256": digest, "httpStatus": response.status_code, "serverSchemaValidated": response.status_code == 200,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "status": body.get("status"), "code": body.get("code"), "modelName": body.get("modelName"), "fields": body.get("fields"),
                "humanTruth": manifest["humanTruth"][name],
            })
            print(f"{name}: HTTP {response.status_code} {body.get('status') or body.get('code')}", flush=True)
            if meter.exhausted:
                break
    report = {"provider": "gemini", "configuredModel": model, "realExternalCall": True, "syntheticImagesOnly": True, "budgetExhausted": meter.exhausted, "sdkCalls": len(meter.calls), "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(item["httpStatus"] == 200 for item in results) and len(results) == len(IMAGES) else 1


if __name__ == "__main__":
    sys.exit(main())
