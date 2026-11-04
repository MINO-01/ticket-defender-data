"""Track 2 실모델 VLM 평가 실행기와 채점 함수.

일반 CI와 unittest는 이 모듈의 채점 함수만 사용하며 외부 모델을 호출하지 않습니다.
실제 호출은 아래 명령처럼 명시적 확인 옵션을 준 별도 실행에서만 일어나고,
고정 manifest의 SHA-256과 일치하는 합성 fixture 외의 파일은 전송하지 않습니다.

    .\\venv\\Scripts\\python.exe -B scripts\\live_vlm_eval.py --confirm-synthetic-external-call --output <report.json>

GEMINI_API_KEY는 환경변수 또는 로컬 .env에서 읽고, 값은 출력하거나 보고서에 기록하지 않습니다.
X-TD-Internal-Key 용 내부 공유키는 실행마다 임시 난수로 만들며 Gemini 키와 별개입니다.
"""

import argparse
import asyncio
import base64
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import unicodedata
import uuid


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "track2_live_vlm"
FIELD_NAMES = ("eventName", "performanceAt", "platformDisplayName", "askingPrice", "currency",
               "saleText", "zone", "row", "seat")
DEFAULT_MAX_CALLS = 14
# 추가 검증별 서비스 제한. 최악의 경우 시도 횟수 합만큼 SDK 호출이 필요합니다.
FAILURE_PROBE_LIMITS = {
    "permanent_unknown_model": {"max_attempts": 3},
    "timeout_bounded_retry": {"max_attempts": 2, "timeout_seconds": 0.05, "backoff_seconds": 0},
}
FAILURE_PROBE_MAX_CALLS = sum(limits["max_attempts"] for limits in FAILURE_PROBE_LIMITS.values())
CONFIRM_FLAG = "--confirm-synthetic-external-call"

# 필드 채점 결과. ACCEPTED_* 만 정답으로 계산합니다.
CORRECT_FOUND = "CORRECT_FOUND"
ACCEPTED_NON_FOUND = "ACCEPTED_NON_FOUND"
WRONG_FOUND = "WRONG_FOUND"
FALSE_FOUND = "FALSE_FOUND"
MISSED = "MISSED"
STATE_MISMATCH = "STATE_MISMATCH"
PASSING_OUTCOMES = frozenset({CORRECT_FOUND, ACCEPTED_NON_FOUND})


def load_manifest(fixture_dir: Path = FIXTURE_DIR) -> list[dict]:
    """manifest를 읽고 각 이미지 바이트의 SHA-256을 검증한 뒤 반환합니다."""
    manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    entries = []
    for entry in manifest["fixtures"]:
        data = (fixture_dir / entry["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
            raise ValueError(f"fixture digest mismatch: {entry['id']}")
        entries.append({**entry, "data": data})
    return entries


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def _seat_token(text: str) -> str:
    return re.sub(r"(구역|열|번|석|zone|row|seat)$", "", _normalize(text))


def _value_matches(spec: dict, value) -> bool:
    match = spec["match"]
    if match == "price":
        try:
            return isinstance(value, dict) and Decimal(value["amount"]) == Decimal(spec["amount"])
        except (InvalidOperation, KeyError, TypeError):
            return False
    if not isinstance(value, str):
        return False
    if match == "contains":
        return any(_normalize(token) in _normalize(value) for token in spec["values"])
    if match == "seat":
        return _seat_token(value) in {_seat_token(token) for token in spec["values"]}
    return _normalize(value) in {_normalize(token) for token in spec["values"]}


def score_field(spec: dict, observation: dict) -> str:
    """정답 명세와 모델 관찰값 하나를 비교해 채점 결과 코드를 반환합니다."""
    state = observation.get("state")
    if state not in spec["accept"]:
        if state == "FOUND":
            return FALSE_FOUND
        if "FOUND" in spec["accept"]:
            return MISSED
        return STATE_MISMATCH
    if state == "FOUND":
        return CORRECT_FOUND if _value_matches(spec, observation.get("value")) else WRONG_FOUND
    return ACCEPTED_NON_FOUND


def score_response(entry: dict, http_status: int, body: dict) -> dict:
    """HTTP 응답 하나를 채점합니다. 2xx가 아니면 필드 채점 없이 실패로 기록합니다."""
    result = {"id": entry["id"], "httpStatus": http_status, "expectedStatus": entry["expectedStatus"]}
    if http_status != 200:
        result.update(status="FAILED", errorCode=body.get("code"), statusAccepted=False, fields={})
        return result
    fields = body["fields"]
    scored = {}
    for name in FIELD_NAMES:
        observation = fields[name]
        scored[name] = {
            "state": observation["state"],
            "value": observation.get("value"),
            "candidates": observation.get("candidates", []),
            "unknownReason": observation.get("unknownReason"),
            "outcome": score_field(entry["expected"][name], observation),
        }
    result.update(
        status=body["status"], statusAccepted=body["status"] in entry["expectedStatus"],
        modelName=body.get("modelName"), modelVersion=body.get("modelVersion"),
        analyzedAtPresent=bool(body.get("analyzedAt")), reviewRequired=body.get("reviewRequired"),
        fields=scored,
    )
    return result


def summarize(results: list[dict]) -> dict:
    """필드별 정확도와 거짓 FOUND 및 누락 건수를 집계합니다."""
    per_field = {}
    for name in FIELD_NAMES:
        outcomes = [result["fields"][name]["outcome"] for result in results if result["fields"]]
        per_field[name] = {
            "scored": len(outcomes),
            "passed": sum(outcome in PASSING_OUTCOMES for outcome in outcomes),
            **{code: outcomes.count(code) for code in (WRONG_FOUND, FALSE_FOUND, MISSED, STATE_MISMATCH)},
        }
    scored = sum(item["scored"] for item in per_field.values())
    passed = sum(item["passed"] for item in per_field.values())
    return {
        "fixtures": len(results),
        "httpSuccess": sum(result["httpStatus"] == 200 for result in results),
        "statusAccepted": sum(result["statusAccepted"] for result in results),
        "fieldAccuracy": round(passed / scored, 4) if scored else None,
        "fieldsPassed": passed,
        "fieldsScored": scored,
        "perField": per_field,
    }


class CallBudgetExceeded(Exception):
    pass


class CallMeter:
    """실제 SDK generate_content 호출 횟수, 지연시간, 토큰 수만 기록합니다. 본문은 기록하지 않습니다."""

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.calls = []
        self.exhausted = False

    def install(self):
        from google.genai import models

        original = models.AsyncModels.generate_content
        meter = self

        async def counted(self_models, *args, **kwargs):
            if len(meter.calls) >= meter.max_calls:
                meter.exhausted = True
                raise CallBudgetExceeded()
            record = {"index": len(meter.calls) + 1, "imageBytesSent": _image_bytes(kwargs.get("contents"))}
            meter.calls.append(record)
            started = time.perf_counter()
            try:
                response = await original(self_models, *args, **kwargs)
            except BaseException as error:
                # 제공자 오류 상태와 짧은 메시지만 남겨 모델과 SDK와 스키마 원인을 구분합니다.
                record.update(latencyMs=round((time.perf_counter() - started) * 1000), error=type(error).__name__,
                              errorCode=getattr(error, "code", None), errorStatus=getattr(error, "status", None),
                              errorMessage=(getattr(error, "message", None) or "")[:300] or None)
                raise
            usage = response.usage_metadata
            candidate = (response.candidates or [None])[0]
            record.update(
                latencyMs=round((time.perf_counter() - started) * 1000),
                finishReason=str(getattr(candidate, "finish_reason", None)),
                promptTokens=getattr(usage, "prompt_token_count", None),
                outputTokens=getattr(usage, "candidates_token_count", None),
                thoughtsTokens=getattr(usage, "thoughts_token_count", None),
                modelVersion=response.model_version,
            )
            return response

        models.AsyncModels.generate_content = counted


def _image_bytes(contents) -> int:
    total = 0
    for part in contents or ():
        inline = getattr(part, "inline_data", None)
        if inline is not None and inline.data:
            total += len(inline.data)
    return total


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        from dotenv import dotenv_values

        key = dotenv_values(Path(__file__).resolve().parent.parent / ".env").get("GEMINI_API_KEY") or ""
    return key


def _request_body(entry: dict) -> dict:
    return {
        "requestId": str(uuid.uuid4()), "evidenceId": str(uuid.uuid4()), "imageSha256": entry["sha256"],
        "mimeType": entry["mimeType"], "imageBase64": base64.b64encode(entry["data"]).decode("ascii"),
    }


def run_route_evaluation(entries: list[dict], internal_key: str, meter: "CallMeter") -> list[dict]:
    """실제 FastAPI 경로와 사전 인증 게이트를 거쳐 fixture를 순차 평가합니다.

    호출 예산이 소진되면 해당 응답은 제공자 실패가 아닌 BUDGET_EXHAUSTED 로 기록하고 평가를 멈춥니다.
    """
    from fastapi.testclient import TestClient
    from main import app

    results = []
    with TestClient(app) as client:
        for entry in entries:
            started = time.perf_counter()
            response = client.post("/api/v1/parse/listing-context", json=_request_body(entry),
                                   headers={"X-TD-Internal-Key": internal_key})
            scored = score_response(entry, response.status_code, response.json())
            scored["routeLatencyMs"] = round((time.perf_counter() - started) * 1000)
            if meter.exhausted:
                scored.update(status="BUDGET_EXHAUSTED", errorCode=None)
            results.append(scored)
            print(f"{entry['id']}: HTTP {response.status_code} {scored['status']}", flush=True)
            if meter.exhausted:
                break
    return results


async def run_failure_probes(entry: dict, api_key: str, model_name: str) -> list[dict]:
    """영구 거부와 시간 초과의 실제 재시도 동작을 서비스 계층에서 확인합니다.

    호출 예산이 도중에 소진되면 예외를 전파하지 않고 BUDGET_EXHAUSTED 로 기록한 뒤 남은 검증을 멈춥니다.
    """
    from core.vlm_errors import VLMProblem
    from services.image_validation import ValidatedImage
    from services.vlm_service import GeminiProvider, VLMService

    image = ValidatedImage(entry["data"], entry["mimeType"], entry["sha256"], entry["width"], entry["height"])
    models = {"permanent_unknown_model": "td-nonexistent-model", "timeout_bounded_retry": model_name}
    results = []
    for name, limits in FAILURE_PROBE_LIMITS.items():
        service = VLMService(GeminiProvider(api_key, models[name]), **limits)
        try:
            await service.parse_listing_context(image)
            results.append({"probe": name, "outcome": "UNEXPECTED_SUCCESS"})
        except VLMProblem as problem:
            results.append({"probe": name, "httpStatus": problem.status_code, "code": problem.code})
        except CallBudgetExceeded:
            results.append({"probe": name, "outcome": "BUDGET_EXHAUSTED"})
            break
        finally:
            await service.close()
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Track 2 합성 fixture 실모델 평가")
    parser.add_argument(CONFIRM_FLAG, dest="confirmed", action="store_true",
                        help="합성 fixture를 외부 Gemini API로 전송하는 것에 동의")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--only", nargs="+", help="평가할 fixture id 목록")
    parser.add_argument("--failure-probes", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirmed:
        print(f"외부 호출 확인 옵션 {CONFIRM_FLAG} 이 없어 실행하지 않습니다.", file=sys.stderr)
        return 2
    if not 1 <= args.max_calls <= 30:
        print("--max-calls 는 1 이상 30 이하만 허용합니다.", file=sys.stderr)
        return 2
    api_key = _api_key()
    if not api_key:
        print("GEMINI_API_KEY 가 없어 실모델 평가를 실행하지 않습니다.", file=sys.stderr)
        return 3

    entries = load_manifest()
    if args.only:
        selected = set(args.only)
        unknown = selected - {entry["id"] for entry in entries}
        if unknown:
            print(f"알 수 없는 fixture id: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        entries = [entry for entry in entries if entry["id"] in selected]
    model_name = os.getenv("VLM_MODEL", "gemini-3.8-flash")
    # 이 프로세스에서만 합성 평가용 게이트를 엽니다. 운영 기본값과 .env 는 변경하지 않습니다.
    internal_key = secrets.token_urlsafe(48)
    os.environ.update({
        "VLM_ENABLED": "true", "VLM_EXTERNAL_CALLS_ENABLED": "true", "VLM_PROVIDER": "gemini",
        "VLM_INTERNAL_SHARED_SECRET": internal_key, "GEMINI_API_KEY": api_key, "VLM_MODEL": model_name,
        "VLM_MAX_CONCURRENT": "1",
    })
    meter = CallMeter(args.max_calls)
    meter.install()

    from google.genai import __version__ as sdk_version

    started = time.perf_counter()
    results = run_route_evaluation(entries, internal_key, meter)
    probes = []
    probe_status = "NOT_REQUESTED"
    if args.failure_probes:
        # 경로 평가가 예산을 정확히 소진해도 exhausted 는 거짓이므로 남은 호출 수로 사전 검사합니다.
        if meter.max_calls - len(meter.calls) < FAILURE_PROBE_MAX_CALLS:
            probe_status = "SKIPPED_INSUFFICIENT_BUDGET"
        else:
            probes = asyncio.run(run_failure_probes(entries[0], api_key, model_name))
            exhausted = any(probe.get("outcome") == "BUDGET_EXHAUSTED" for probe in probes)
            probe_status = "BUDGET_EXHAUSTED" if exhausted else "COMPLETED"
    report = {
        "model": model_name, "sdk": f"google-genai {sdk_version}", "python": sys.version.split()[0],
        "elapsedSeconds": round(time.perf_counter() - started, 2), "maxCalls": args.max_calls,
        "budgetExhausted": meter.exhausted,
        "summary": summarize(results), "results": results, "failureProbes": probes,
        "failureProbesStatus": probe_status, "calls": meter.calls,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"SDK calls: {len(meter.calls)}, report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
