"""검증된 합성 원장 v2에서 구매 한도 분석 v2 시연 입력을 별도 산출물로 만듭니다.

원장에는 구매 시각이 없으므로 purchasedAt은 호출자가 명시한 합성 전제이며
실제 예매 기록에서 추출한 값이 아닙니다. 실제 운영 입력은 Spring이 원천 예매 데이터와
서버 검증 정책으로 만들어야 하며 이 모듈을 쓰지 않습니다.
정답 파일은 읽지 않고, 원장 v2의 파일과 manifest는 수정하지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import uuid

from schemas.dto import Track1LimitAnalysisRequest

from . import ledger_v2
from .ledger import MANIFEST_FILE, TRACK1_FILE, _json_bytes, require_outside_repository


LIMIT_INPUT_FILE = "track1_limit_input_v2.json"
LIMIT_INPUT_MANIFEST_FILE = "track1_limit_input_manifest.json"
_COMPLETENESS_REASON = "SYNTHETIC_SNAPSHOT_ONE_ROW_ONE_TICKET_NO_CANCELLATION_OR_DUPLICATE_GENERATED"


@dataclass(frozen=True)
class RegisteredPolicy:
    """격리 환경의 Spring에 실제 등록된 정책 값을 호출자가 그대로 전달합니다."""

    max_tickets_per_account: int
    policy_version: str
    effective_from: str
    effective_until: str | None = None


def _build_scope(
    manifest: dict, internal_event_id: str, quantity_as_of: str, policy: RegisteredPolicy | None
) -> dict[str, object]:
    return {
        "internalEventId": internal_event_id,
        "eventId": manifest["eventRef"],
        "performanceId": manifest["performanceId"],
        "policyVerificationStatus": "CONFIRMED" if policy else "POLICY_NOT_CONFIRMED",
        "policy": None if policy is None else {
            "maxTicketsPerAccount": policy.max_tickets_per_account,
            "policyVersion": policy.policy_version,
            "effectiveFrom": policy.effective_from,
            "effectiveUntil": policy.effective_until,
        },
        "quantityDataStatus": "COMPLETE",
        "quantitySourceVersion": manifest["sourceVersion"],
        "quantityAsOf": quantity_as_of,
        "cancellationHandling": "NETTED",
        "duplicateHandling": "DEDUPLICATED",
        "dataCompletenessReason": _COMPLETENESS_REASON,
    }


def build_limit_input(
    ledger_dir: Path, *, internal_event_id: str, synthetic_purchased_at: str, quantity_as_of: str,
    policy: RegisteredPolicy | None = None, require_committed_generator: bool = True,
    allow_unconfirmed_ledger: bool = False,
) -> tuple[bytes, bytes]:
    """원장 검증을 통과한 경우에만 입력과 manifest 바이트를 만듭니다."""

    verification = ledger_v2.verify(ledger_dir, require_committed_generator)
    if verification["status"] != "PASS":
        raise ValueError("Ledger v2 verification failed")
    if not verification["finalForImport"] and not allow_unconfirmed_ledger:
        raise ValueError("Ledger v2 must be frozen with a confirmed performance_id")

    manifest_bytes = (ledger_dir / MANIFEST_FILE).read_bytes()
    manifest = json.loads(manifest_bytes)
    track1_bytes = (ledger_dir / TRACK1_FILE).read_bytes()
    tickets = [
        {
            **ticket,
            "performanceId": manifest["performanceId"],
            "purchasedAt": synthetic_purchased_at,
            "effectiveTicketQuantity": 1,
        }
        for ticket in json.loads(track1_bytes)["tickets"]
    ]
    identity = f"track1-limit-input:{sha256(track1_bytes).hexdigest()}:{internal_event_id}:{policy}"
    payload = {
        "requestId": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
        "performanceScopes": [_build_scope(manifest, internal_event_id, quantity_as_of, policy)],
        "tickets": tickets,
    }
    Track1LimitAnalysisRequest.model_validate(payload)
    input_bytes = _json_bytes(payload)
    return input_bytes, _json_bytes({
        "kind": "SYNTHETIC_TRACK1_LIMIT_INPUT_V2",
        "finalForDemo": bool(verification["finalForImport"]) and policy is not None,
        "inputSha256": sha256(input_bytes).hexdigest(),
        "sourceLedger": {
            "sourceVersion": manifest["sourceVersion"],
            "manifestSha256": sha256(manifest_bytes).hexdigest(),
            "rowsSha256": manifest["ledger"]["rowsSha256"],
            "track1InputSha256": sha256(track1_bytes).hexdigest(),
            "finalForImport": verification["finalForImport"],
        },
        "generatorCodeSha256": _generator_code_sha256(),
        "performanceId": manifest["performanceId"],
        "policyStatus": "CALLER_SUPPLIED_REGISTERED_POLICY" if policy else "POLICY_NOT_CONFIRMED",
        "syntheticAssumptions": {
            "purchasedAt": {"value": synthetic_purchased_at, "fromLedger": False},
            "effectiveTicketQuantity": {"value": 1, "basis": "ONE_ROW_IS_ONE_VALID_TICKET_SYNTHETIC_ASSUMPTION"},
            "quantityAsOf": {"value": quantity_as_of, "fromLedger": False},
            "cancellationAndDuplicateHandling": _COMPLETENESS_REASON,
        },
        "independence": {"readsAnswerKey": False, "containsAnalysisResults": False, "externalModelCalls": 0},
    })


def _generator_code_sha256() -> str:
    digest = sha256(ledger_v2.generator_code_sha256().encode("ascii"))
    digest.update(Path(__file__).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def write_limit_input(ledger_dir: Path, output_dir: Path, **options) -> Path:
    """원장과 다른 빈 디렉터리에만 기록합니다. 기존 산출물은 덮어쓰지 않습니다."""

    target = require_outside_repository(output_dir)
    if target == ledger_dir.resolve() or ledger_dir.resolve() in target.parents:
        raise ValueError("Limit input must be written outside the ledger directory")
    if target.exists() and any(target.iterdir()):
        raise FileExistsError("Output directory is not empty")
    input_bytes, manifest_bytes = build_limit_input(ledger_dir, **options)
    target.mkdir(parents=True, exist_ok=True)
    with (target / LIMIT_INPUT_FILE).open("xb") as output:
        output.write(input_bytes)
    with (target / LIMIT_INPUT_MANIFEST_FILE).open("xb") as output:
        output.write(manifest_bytes)
    return target / LIMIT_INPUT_MANIFEST_FILE
