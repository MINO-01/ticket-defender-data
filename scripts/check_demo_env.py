"""시연 환경의 Gemini와 내부 인증 설정을 값 출력 없이 점검합니다.

    .\\venv\\Scripts\\python.exe -B scripts\\check_demo_env.py [--backend-env ..\\ticket-defender-backend\\.env]

비밀값은 출력하거나 파일에 쓰지 않습니다. 존재 여부, 최소 길이, 두 값의 일치 여부만 표시합니다.
GEMINI_API_KEY가 없으면 GEMINI_KEY_NOT_CONFIGURED를 표시합니다.
"""

import argparse
import hmac
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
MIN_SECRET_BYTES = 32
REQUIRED_FLAGS = {"VLM_ENABLED": "true", "VLM_EXTERNAL_CALLS_ENABLED": "true", "VLM_PROVIDER": "gemini"}


def check(data_env: dict, backend_env: dict | None) -> tuple[list[str], bool]:
    """표시할 줄과 전체 통과 여부를 돌려줍니다. 비밀값은 결과에 포함하지 않습니다."""

    lines = []
    passed = True
    for name, expected in REQUIRED_FLAGS.items():
        ok = (data_env.get(name) or "").lower() == expected
        passed &= ok
        lines.append(f"{name}: {'OK' if ok else 'NOT_SET_AS_REQUIRED'}")
    model = data_env.get("VLM_MODEL") or ""
    passed &= bool(model)
    lines.append(f"VLM_MODEL: {model or 'NOT_SET'}")
    gemini = data_env.get("GEMINI_API_KEY") or ""
    passed &= bool(gemini)
    lines.append(f"GEMINI_API_KEY: {'PRESENT' if gemini else 'GEMINI_KEY_NOT_CONFIGURED'}")
    data_secret = data_env.get("VLM_INTERNAL_SHARED_SECRET") or ""
    data_ok = len(data_secret.encode("utf-8")) >= MIN_SECRET_BYTES
    passed &= data_ok
    lines.append(f"VLM_INTERNAL_SHARED_SECRET: {'PRESENT' if data_ok else 'MISSING_OR_TOO_SHORT'}")
    if backend_env is None:
        lines.append("BACKEND_ENV: NOT_CHECKED")
        return lines, passed

    backend_secret = backend_env.get("TRACK2_LISTING_ANALYSIS_INTERNAL_KEY") or ""
    same = bool(data_secret) and hmac.compare_digest(data_secret.encode("utf-8"), backend_secret.encode("utf-8"))
    passed &= same
    lines.append(f"INTERNAL_KEY_MATCH: {'MATCH' if same else 'MISMATCH'}")
    enabled = (backend_env.get("TRACK2_LISTING_ANALYSIS_ENABLED") or "").lower() == "true"
    passed &= enabled
    lines.append(f"TRACK2_LISTING_ANALYSIS_ENABLED: {'OK' if enabled else 'NOT_TRUE'}")
    url = backend_env.get("TRACK2_LISTING_ANALYSIS_URL") or ""
    parts = urlsplit(url)
    url_ok = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"})
    passed &= url_ok
    lines.append(f"TRACK2_LISTING_ANALYSIS_URL: {url if url_ok else 'MISSING_OR_NOT_ALLOWED'}")
    return lines, passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="시연 환경 설정을 값 출력 없이 점검")
    parser.add_argument("--data-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--backend-env", type=Path)
    args = parser.parse_args(argv)
    backend = dotenv_values(args.backend_env) if args.backend_env else None
    lines, passed = check(dotenv_values(args.data_env), backend)
    print("\n".join(lines))
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
