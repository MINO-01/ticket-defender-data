"""사용자가 제공할 실제 웹 캡처 4건의 평가 기록 템플릿과 스키마입니다.

템플릿은 빈 슬롯만 만들며 이미지나 평가 결과를 생성하지 않습니다.
정답은 사람이 이미지를 읽어 기록하고, 모델 출력이나 원장 결과를 정답으로 재사용하지 않습니다.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from schemas.listing_context import ListingFields

from .ledger import MANIFEST_FILE, require_outside_repository, verify


CAPTURE_IDS = ("CAPTURE-A", "CAPTURE-B", "CAPTURE-C", "CAPTURE-D")
FUNCTION_TEST_SELECTION = "USER_SELECTED_SEAT_FROM_FROZEN_LEDGER_FUNCTION_TEST"
NO_INPUT_CHANGE = "NO_INPUT_CHANGE_AFTER_POST_AUTHORED"
EXPECTED_SELECTION_KINDS = {
    capture_id: FUNCTION_TEST_SELECTION if capture_id == "CAPTURE-A" else NO_INPUT_CHANGE
    for capture_id in CAPTURE_IDS
}
TEMPLATE_FILE = "capture_evaluation_template.json"

_Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HumanField(_Strict):
    """사람이 이미지에서 직접 읽은 원문입니다."""

    visibility: Literal["VISIBLE", "ABSENT", "UNREADABLE", "AMBIGUOUS"]
    rawText: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def check_visibility(self):
        if self.visibility in ("VISIBLE", "AMBIGUOUS") and self.rawText is None:
            raise PydanticCustomError("missing_raw_text", "Readable fields need the human-read text")
        if self.visibility in ("ABSENT", "UNREADABLE") and self.rawText is not None:
            raise PydanticCustomError("unexpected_raw_text", "Absent or unreadable fields cannot have text")
        return self


class HumanObservation(_Strict):
    recordedBy: str = Field(min_length=1, max_length=64, description="실제 계정이 아닌 평가자 역할 표시")
    recordedAt: datetime
    recordedBeforeModelOutput: Literal[True]
    eventName: HumanField
    performanceAt: HumanField
    platformDisplayName: HumanField
    askingPrice: HumanField
    currency: HumanField
    saleText: HumanField
    zone: HumanField
    row: HumanField
    seat: HumanField


class ModelEvaluation(_Strict):
    status: Literal["COMPLETED", "PARTIAL", "UNVERIFIABLE", "FAILED"]
    fields: ListingFields | None
    modelName: str | None = Field(min_length=1, max_length=128)
    modelVersion: str | None = Field(max_length=128)
    errorCode: str | None = Field(min_length=1, max_length=128)
    durationMs: int = Field(ge=0)
    failureCount: int = Field(ge=0)
    retryCount: int = Field(ge=0)
    externalSdkCallCount: int = Field(ge=0)

    @model_validator(mode="after")
    def check_outcome(self):
        # 분석 실패를 정상 빈 결과로 바꾸지 않도록 실패와 성공의 필드를 분리합니다.
        if self.status == "FAILED":
            if self.fields is not None or self.errorCode is None:
                raise PydanticCustomError("invalid_failure", "FAILED needs an error code and no fields")
        elif self.fields is None or self.modelName is None or self.errorCode is not None:
            raise PydanticCustomError("invalid_result", "Model results need fields and a model name")
        return self


class LedgerComparison(_Strict):
    sourceVersion: str = Field(min_length=1, max_length=128)
    candidateStatus: Literal[
        "IN_PROGRESS", "MATCH_CANDIDATE", "NO_MATCH", "AMBIGUOUS",
        "UNVERIFIABLE", "LOOKUP_FAILED", "ANALYSIS_FAILED",
    ]


class CaptureRecord(_Strict):
    captureId: Literal["CAPTURE-A", "CAPTURE-B", "CAPTURE-C", "CAPTURE-D"]
    seatSelectionKind: Literal[
        "USER_SELECTED_SEAT_FROM_FROZEN_LEDGER_FUNCTION_TEST", "NO_INPUT_CHANGE_AFTER_POST_AUTHORED"
    ]
    status: Literal["PENDING_CAPTURE", "HUMAN_LABELED", "MODEL_EVALUATED"]
    imageSha256: str | None = _Sha256
    capturedAt: datetime | None
    humanObservation: HumanObservation | None
    modelEvaluation: ModelEvaluation | None
    ledgerComparison: LedgerComparison | None

    @model_validator(mode="after")
    def check_status(self):
        # 수동 편집으로 기능 시험과 무수정 확인의 구분이 바뀌지 않도록 고정합니다.
        if self.seatSelectionKind != EXPECTED_SELECTION_KINDS[self.captureId]:
            raise PydanticCustomError("invalid_selection_kind", "Selection kind is fixed for each capture")
        captured = (self.imageSha256, self.capturedAt, self.humanObservation)
        if self.status == "PENDING_CAPTURE":
            if any(value is not None for value in (*captured, self.modelEvaluation, self.ledgerComparison)):
                raise PydanticCustomError("premature_result", "Pending captures cannot hold results")
            return self
        if any(value is None for value in captured):
            raise PydanticCustomError("missing_capture", "Labeled captures need hash, time and human reading")
        if self.status == "HUMAN_LABELED" and (self.modelEvaluation or self.ledgerComparison):
            raise PydanticCustomError("premature_result", "Human labels must exist before model evaluation")
        if self.status == "MODEL_EVALUATED" and (self.modelEvaluation is None or self.ledgerComparison is None):
            raise PydanticCustomError("missing_result", "Evaluated captures need model and ledger results")
        return self


class CaptureEvaluation(_Strict):
    schemaVersion: Literal[1]
    evaluationKind: Literal["REAL_WEB_CAPTURE_FUNCTION_CHECK"]
    isDetectionAccuracyExperiment: Literal[False]
    independentOcrAccuracyClaimed: Literal[False]
    ledgerSourceVersion: str = Field(min_length=1, max_length=128)
    ledgerManifestSha256: str = _Sha256
    records: list[CaptureRecord] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def check_records(self):
        if tuple(record.captureId for record in self.records) != CAPTURE_IDS:
            raise PydanticCustomError("invalid_capture_ids", "Exactly captures A to D are required")
        if any(
            record.ledgerComparison is not None
            and record.ledgerComparison.sourceVersion != self.ledgerSourceVersion
            for record in self.records
        ):
            raise PydanticCustomError("source_version_mismatch", "Comparison used a different ledger")
        return self


def build_template(manifest_bytes: bytes) -> dict[str, object]:
    """고정 원장 manifest에 묶인 PENDING 슬롯 4개를 만듭니다."""

    manifest = json.loads(manifest_bytes)
    template = {
        "schemaVersion": 1,
        "evaluationKind": "REAL_WEB_CAPTURE_FUNCTION_CHECK",
        "isDetectionAccuracyExperiment": False,
        "independentOcrAccuracyClaimed": False,
        "ledgerSourceVersion": manifest["sourceVersion"],
        "ledgerManifestSha256": sha256(manifest_bytes).hexdigest(),
        "records": [
            {
                "captureId": capture_id,
                "seatSelectionKind": EXPECTED_SELECTION_KINDS[capture_id],
                "status": "PENDING_CAPTURE",
                "imageSha256": None,
                "capturedAt": None,
                "humanObservation": None,
                "modelEvaluation": None,
                "ledgerComparison": None,
            }
            for capture_id in CAPTURE_IDS
        ],
    }
    validate_evaluation(template)
    return template


def validate_evaluation(document: dict[str, object]) -> CaptureEvaluation:
    # JSON 왕복 값을 검사하므로 날짜 문자열 변환만 허용합니다.
    return CaptureEvaluation.model_validate_json(json.dumps(document, ensure_ascii=False))


def write_template(ledger_dir: Path, output_dir: Path, require_committed_generator: bool = True) -> Path:
    """verify가 PASS인 원장에만 평가 템플릿을 연결합니다."""

    target = require_outside_repository(output_dir)
    result = verify(ledger_dir, require_committed_generator=require_committed_generator)
    if result["status"] != "PASS":
        failed = ", ".join(name for name, passed in result["checks"].items() if not passed)
        raise ValueError(f"Ledger verification failed: {failed}")
    template = build_template((ledger_dir / MANIFEST_FILE).read_bytes())
    target.mkdir(parents=True, exist_ok=True)
    path = target / TEMPLATE_FILE
    with path.open("xb") as output:
        output.write((json.dumps(template, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return path
