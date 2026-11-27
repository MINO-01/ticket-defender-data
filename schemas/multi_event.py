"""다공연 조사 분석의 내부 계약입니다.

이 계약은 Spring의 인증된 호출만 받습니다. tenant 값은 사용자 입력으로 신뢰하지 않으며,
원본 식별자나 원본 IP를 받지 않습니다.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints, ValidationInfo, model_validator,
)
from pydantic_core import PydanticCustomError

from schemas.dto import BulkId, HashToken, IpNetworkType, UUIDText


ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
RequestScoreVersion = Literal[
    "score-v2-exp2-multi-performance-recurrence",
    "score-v2-exp2-multi-event-recurrence",
]
RETIRED_SCORE_VERSIONS = frozenset({"score-v2-exp2-multi-event-recurrence"})
# 저장된 작업 manifest를 다시 읽을 때만 퇴역 점수 버전을 허용하는 검증 context 키입니다.
STORED_CONTEXT = {"allowStoredRetiredScoreVersion": True}


def _reject_retired_score_version(score_version: str, info: ValidationInfo) -> None:
    """퇴역한 다공연 점수 버전은 새 분석 요청에서 거절합니다."""
    context = info.context or {}
    if score_version in RETIRED_SCORE_VERSIONS and not context.get("allowStoredRetiredScoreVersion"):
        raise PydanticCustomError("SCORE_VERSION_RETIRED", "Retired multi-event score version")


def _require_distinct_performances(scopes) -> None:
    """반복 근거는 서로 다른 회차 쌍에서만 만들므로 회차 2개 이상이 필요합니다. event는 1개여도 됩니다."""
    if len({scope.performance_id for scope in scopes}) < 2:
        raise PydanticCustomError("multi_event_scope_required", "At least two distinct performances are required")


def _java_string_sort_key(value: str) -> bytes:
    """Match Java String.compareTo ordering for the ASCII/Unicode scope IDs."""
    return value.encode("utf-16-be", errors="surrogatepass")


def _java_length_prefixed_sha256(*parts: str) -> str:
    canonical = "".join(
        f"{len(part.encode('utf-16-le', errors='surrogatepass')) // 2}:{part}"
        for part in parts
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


class MultiEventLedgerScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: ShortText = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    ledger_version_id: UUIDText = Field(alias="ledgerVersionId")
    source_batch_id: ShortText = Field(alias="sourceBatchId")
    policy_snapshot_id: UUIDText | None = Field(default=None, alias="policySnapshotId")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.event_id, self.performance_id, self.ledger_version_id


class MultiEventArtifactLedgerScope(MultiEventLedgerScope):
    """Artifact digest binding also includes Spring's internal event UUID."""

    model_config = ConfigDict(extra="forbid")

    source_event_id: UUIDText = Field(alias="sourceEventId")


class MultiEventTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: HashToken = Field(alias="accountId")
    reservation_no: HashToken = Field(alias="reservationNo")
    event_id: ShortText = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    ledger_version_id: UUIDText = Field(alias="ledgerVersionId")
    supplier_namespace: ShortText = Field(alias="supplierNamespace")
    hmac_purpose: Literal["TRACK1_CONNECTION"] = Field(alias="hmacPurpose")
    hmac_key_version: ShortText = Field(alias="hmacKeyVersion")
    source_version: ShortText = Field(alias="sourceVersion")
    source_row_ordinal: StrictInt | None = Field(default=None, alias="sourceRowOrdinal", ge=1, le=400_000)
    purchased_at: AwareDatetime | None = Field(default=None, alias="purchasedAt")
    effective_ticket_quantity: StrictInt | None = Field(default=None, alias="effectiveTicketQuantity", ge=1, le=100)
    confirmed_purchase_limit: StrictInt | None = Field(default=None, alias="confirmedPurchaseLimit", ge=1, le=100)
    quantity_policy_snapshot_id: UUIDText | None = Field(default=None, alias="quantityPolicySnapshotId")
    quantity_policy_version: ShortText | None = Field(default=None, alias="quantityPolicyVersion")
    quantity_source_version: ShortText | None = Field(default=None, alias="quantitySourceVersion")
    payment_hash: HashToken | None = Field(default=None, alias="paymentHash")
    address_hash: HashToken | None = Field(default=None, alias="addressHash")
    device_id_hash: HashToken | None = Field(default=None, alias="deviceIdHash")
    ip_hash: HashToken | None = Field(default=None, alias="ipHash")
    refund_account_hash: HashToken | None = Field(default=None, alias="refundAccountHash")
    phone_hash: HashToken | None = Field(default=None, alias="phoneHash")
    ip_network_type: IpNetworkType | None = Field(default=None, alias="ipNetworkType")
    quantity_status: Literal["COMPLETE", "INCOMPLETE", "POLICY_NOT_CONFIRMED", "UNKNOWN"] = Field(
        default="UNKNOWN", alias="quantityStatus"
    )
    quantity_reason: LongText | None = Field(default=None, alias="quantityReason")

    @model_validator(mode="after")
    def validate_tokens(self):
        if self.ip_network_type is not None and self.ip_hash is None:
            raise PydanticCustomError("invalid_ip_network_type", "ipNetworkType requires ipHash")
        if not any((self.payment_hash, self.address_hash, self.device_id_hash, self.ip_hash,
                    self.refund_account_hash, self.phone_hash)):
            raise PydanticCustomError("missing_connection_token", "At least one connection token is required")
        if self.quantity_status == "COMPLETE" and (
            self.effective_ticket_quantity is None or self.confirmed_purchase_limit is None
            or self.quantity_policy_snapshot_id is None or self.quantity_policy_version is None
            or self.quantity_source_version is None
        ):
            raise PydanticCustomError("incomplete_quantity_provenance", "Verified quantity requires a confirmed policy snapshot and source provenance")
        if self.quantity_status != "COMPLETE" and self.confirmed_purchase_limit is not None:
            raise PydanticCustomError("unverified_quantity_limit", "Unverified quantity cannot carry a confirmed policy limit")
        return self

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return self.event_id, self.performance_id, self.ledger_version_id

    @property
    def analysis_scope(self) -> str:
        return f"{self.event_id}|{self.performance_id}"

    @property
    def row_key(self) -> tuple[str, str, str, int | None]:
        return self.account_id, self.reservation_no, self.ledger_version_id, self.source_row_ordinal


class MultiEventCancelledTicketInput(BaseModel):
    """Separate pseudonymous cancellation evidence; it is never an active ticket row."""

    model_config = ConfigDict(extra="forbid")

    account_hmac: HashToken = Field(alias="accountHmac")
    ticket_hmac: HashToken = Field(alias="ticketHmac")
    event_id: ShortText = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    ledger_version_id: UUIDText = Field(alias="ledgerVersionId")
    supplier_namespace: ShortText = Field(alias="supplierNamespace")
    hmac_purpose: Literal["TRACK1_CONNECTION"] = Field(alias="hmacPurpose")
    hmac_key_version: ShortText = Field(alias="hmacKeyVersion")
    source_version: ShortText = Field(alias="sourceVersion")
    source_row_ordinal: StrictInt = Field(alias="sourceRowOrdinal", ge=1, le=400_000)
    seat_zone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = Field(default=None, alias="seatZone")
    seat_row: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = Field(default=None, alias="seatRow")
    seat_number: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = Field(default=None, alias="seatNumber")
    cancelled_at: AwareDatetime = Field(alias="cancelledAt")

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return self.event_id, self.performance_id, self.ledger_version_id

    @property
    def row_key(self) -> tuple[str, str, str, int]:
        return self.account_hmac, self.ticket_hmac, self.ledger_version_id, self.source_row_ordinal


class MultiEventArtifactCancelledTicketRecord(BaseModel):
    """Version 2 NDJSON envelope. Cancellation rows never enter the active row pipeline."""

    model_config = ConfigDict(extra="forbid")

    record_type: Literal["CANCELLED_TICKET"] = Field(alias="recordType")
    cancelled_ticket: MultiEventCancelledTicketInput = Field(alias="cancelledTicket")


class MultiEventAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: UUIDText = Field(alias="analysisId")
    tenant_id: UUIDText = Field(alias="tenantId")
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    analysis_as_of: AwareDatetime = Field(alias="analysisAsOf")
    analysis_start_at: AwareDatetime = Field(alias="analysisStartAt")
    analysis_end_at: AwareDatetime = Field(alias="analysisEndAt")
    score_version: RequestScoreVersion = Field(alias="scoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    # Keep the small JSON compatibility contract unchanged. sourceEventId is
    # required only by the immutable artifact manifest below.
    ledger_scopes: list[MultiEventLedgerScope] = Field(alias="ledgerScopes", min_length=2, max_length=500)
    tickets: list[MultiEventTicketInput] = Field(min_length=1, max_length=50_000)
    cancelled_tickets: list[MultiEventCancelledTicketInput] = Field(
        default_factory=list, alias="cancelledTickets", max_length=50_000
    )

    @model_validator(mode="after")
    def validate_fixed_scope(self, info: ValidationInfo):
        _reject_retired_score_version(self.score_version, info)
        if self.analysis_end_at <= self.analysis_start_at or self.analysis_end_at > self.analysis_as_of:
            raise PydanticCustomError("invalid_analysis_period", "Analysis period must be ordered and not after analysisAsOf")
        scope_keys = [scope.key for scope in self.ledger_scopes]
        if len(scope_keys) != len(set(scope_keys)):
            raise PydanticCustomError("duplicate_ledger_scope", "Ledger scopes must be unique")
        _require_distinct_performances(self.ledger_scopes)
        scope_set = set(scope_keys)
        scopes_by_key = {scope.key: scope for scope in self.ledger_scopes}
        if any(ticket.scope_key not in scope_set for ticket in self.tickets):
            raise PydanticCustomError("ticket_scope_mismatch", "Ticket scope must be fixed by ledgerScopes")
        if any(ticket.scope_key not in scope_set for ticket in self.cancelled_tickets):
            raise PydanticCustomError("cancelled_ticket_scope_mismatch", "Cancelled ticket scope must be fixed by ledgerScopes")
        if any(ticket.quantity_status == "COMPLETE"
               and ticket.quantity_policy_snapshot_id != scopes_by_key[ticket.scope_key].policy_snapshot_id
               for ticket in self.tickets):
            raise PydanticCustomError("quantity_snapshot_scope_mismatch", "Quantity evidence must use the snapshot fixed by its ledger scope")
        row_keys = [ticket.row_key for ticket in self.tickets]
        if len(row_keys) != len(set(row_keys)):
            raise PydanticCustomError("duplicate_ticket", "Duplicate ticket identity is not allowed")
        row_ordinals = [(ticket.scope_key, ticket.source_row_ordinal) for ticket in self.tickets
                        if ticket.source_row_ordinal is not None]
        if len(row_ordinals) != len(set(row_ordinals)):
            raise PydanticCustomError("duplicate_source_row", "Source row ordinals must be unique within each ledger scope")
        cancelled_row_keys = [ticket.row_key for ticket in self.cancelled_tickets]
        if len(cancelled_row_keys) != len(set(cancelled_row_keys)):
            raise PydanticCustomError("duplicate_cancelled_ticket", "Duplicate cancelled ticket evidence is not allowed")
        cancelled_row_ordinals = [(ticket.scope_key, ticket.source_row_ordinal) for ticket in self.cancelled_tickets]
        if (len(cancelled_row_ordinals) != len(set(cancelled_row_ordinals))
                or set(row_ordinals).intersection(cancelled_row_ordinals)):
            raise PydanticCustomError("duplicate_source_row", "Source row ordinals must be unique across active and cancelled records")
        identity_rows = [*self.tickets, *self.cancelled_tickets]
        suppliers = {ticket.supplier_namespace for ticket in identity_rows}
        key_versions = {ticket.hmac_key_version for ticket in identity_rows}
        purposes = {ticket.hmac_purpose for ticket in identity_rows}
        if len(suppliers) != 1 or len(key_versions) != 1 or purposes != {"TRACK1_CONNECTION"}:
            raise PydanticCustomError("identity_contract_mixed", "Supplier, purpose, and key version must be uniform")
        return self


class MultiEventArtifactManifest(BaseModel):
    """Immutable multi-event NDJSON artifact and fixed analysis input contract.

    The row payload lives in the configured private object store. This manifest
    intentionally accepts an opaque object name only; it never accepts a URL,
    bucket name, or caller-selected path.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: BulkId = Field(alias="artifactId")
    object_key: BulkId = Field(alias="objectKey")
    object_version: BulkId = Field(alias="objectVersion")
    sha256: HashToken
    record_count: StrictInt = Field(alias="recordCount", ge=1, le=400_000)
    cancelled_ticket_record_count: StrictInt = Field(default=0, alias="cancelledTicketRecordCount", ge=0, le=399_999)
    input_format_version: Literal["multi-event-exp2-ndjson-v1", "multi-event-exp2-ndjson-v2"] = Field(alias="inputFormatVersion")
    analysis_id: UUIDText = Field(alias="analysisId")
    tenant_id: UUIDText = Field(alias="tenantId")
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    analysis_as_of: AwareDatetime = Field(alias="analysisAsOf")
    analysis_start_at: AwareDatetime = Field(alias="analysisStartAt")
    analysis_end_at: AwareDatetime = Field(alias="analysisEndAt")
    score_version: RequestScoreVersion = Field(alias="scoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    supplier_namespace: ShortText = Field(alias="supplierNamespace")
    hmac_purpose: Literal["TRACK1_CONNECTION"] = Field(alias="hmacPurpose")
    hmac_key_version: ShortText = Field(alias="hmacKeyVersion")
    ledger_scopes: list[MultiEventArtifactLedgerScope] = Field(alias="ledgerScopes", min_length=2, max_length=500)

    @model_validator(mode="after")
    def validate_manifest(self, info: ValidationInfo):
        _reject_retired_score_version(self.score_version, info)
        if self.cancelled_ticket_record_count >= self.record_count:
            raise PydanticCustomError("INVALID_CANCELLED_TICKET_COUNT", "artifact must contain at least one active ticket row")
        if self.input_format_version == "multi-event-exp2-ndjson-v1" and self.cancelled_ticket_record_count != 0:
            raise PydanticCustomError("INVALID_CANCELLED_TICKET_FORMAT", "cancelled ticket rows require NDJSON v2")
        if any(separator in self.object_key for separator in ("/", "\\", ":")):
            raise PydanticCustomError("INVALID_ARTIFACT_REFERENCE", "objectKey must be an opaque object name")
        if self.analysis_end_at <= self.analysis_start_at or self.analysis_end_at > self.analysis_as_of:
            raise PydanticCustomError("invalid_analysis_period", "Analysis period must be ordered and not after analysisAsOf")
        scope_keys = [scope.key for scope in self.ledger_scopes]
        if len(scope_keys) != len(set(scope_keys)):
            raise PydanticCustomError("duplicate_ledger_scope", "Ledger scopes must be unique")
        _require_distinct_performances(self.ledger_scopes)
        internal_to_external: dict[str, str] = {}
        external_to_internal: dict[str, str] = {}
        for scope in self.ledger_scopes:
            if (scope.source_event_id in internal_to_external
                    and internal_to_external[scope.source_event_id] != scope.event_id):
                raise PydanticCustomError("event_scope_binding_mismatch", "sourceEventId must map to one external event")
            if (scope.event_id in external_to_internal
                    and external_to_internal[scope.event_id] != scope.source_event_id):
                raise PydanticCustomError("event_scope_binding_mismatch", "external eventId must map to one sourceEventId")
            internal_to_external[scope.source_event_id] = scope.event_id
            external_to_internal[scope.event_id] = scope.source_event_id
        canonical_rows = [
            f"{scope.source_event_id}|{scope.performance_id}|{scope.ledger_version_id}"
            for scope in sorted(self.ledger_scopes,
                                key=lambda item: (_java_string_sort_key(item.source_event_id),
                                                  _java_string_sort_key(item.performance_id),
                                                  _java_string_sort_key(item.ledger_version_id)))
        ]
        expected_digest = _java_length_prefixed_sha256("MEI_LEDGER_VERSION_SET", "\n".join(canonical_rows))
        if self.ledger_version_set_digest != expected_digest:
            raise PydanticCustomError("ledger_version_set_digest_mismatch", "ledgerVersionSetDigest does not match fixed scopes")
        return self


class MultiEventArtifactJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: MultiEventArtifactManifest


class MultiEventArtifactJobAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUIDText = Field(alias="jobId")
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "INPUT_REJECTED", "INTERRUPTED"]
    existing: bool = False


class MultiEventArtifactJobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUIDText = Field(alias="jobId")
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "INPUT_REJECTED", "INTERRUPTED"]
    input_record_count: StrictInt = Field(alias="inputRecordCount", ge=0, le=400_000)
    processed_record_count: StrictInt = Field(alias="processedRecordCount", ge=0, le=400_000)
    candidate_count: StrictInt = Field(alias="candidateCount", ge=0, le=10_000)
    algorithm_version: ShortText | None = Field(default=None, alias="algorithmVersion")
    failure_code: ShortText | None = Field(default=None, alias="failureCode")
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    created_at: AwareDatetime = Field(alias="createdAt")
    updated_at: AwareDatetime = Field(alias="updatedAt")


class MultiEventScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link: float = Field(alias="L", ge=0, le=1, allow_inf_nan=False)
    size: float = Field(alias="G", ge=0, le=1, allow_inf_nan=False)
    behavior: float = Field(alias="B", ge=0, le=1, allow_inf_nan=False)
    quantity: float = Field(alias="Q", ge=0, le=1, allow_inf_nan=False)
    recurrence: float = Field(alias="R", ge=0, le=1, allow_inf_nan=False)
    score: float = Field(alias="score", ge=0, le=1, allow_inf_nan=False)
    quantity_status: str = Field(alias="quantityStatus", max_length=64)
    quantity_reason: str | None = Field(default=None, alias="quantityReason", max_length=512)


class MultiEventCandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(alias="candidateId", min_length=1, max_length=128)
    priority_band: Literal["HIGH", "MEDIUM", "LOW"] = Field(alias="priorityBand")
    # 이전 점수 버전으로 저장된 작업 결과도 읽기 전용으로 조회할 수 있어야 합니다.
    score_version: RequestScoreVersion = Field(alias="scoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    distinct_account_count: StrictInt = Field(alias="distinctAccountCount", ge=2)
    distinct_event_count: StrictInt = Field(alias="distinctEventCount", ge=1)
    distinct_performance_count: StrictInt = Field(alias="distinctPerformanceCount", ge=2)
    verified_relationship_count: StrictInt = Field(alias="verifiedRelationshipCount", ge=1)
    source_scopes: list[MultiEventLedgerScope] = Field(alias="sourceScopes", min_length=2, max_length=500)
    available_signals: list[str] = Field(alias="availableSignals", max_length=10)
    excluded_signals: list[str] = Field(alias="excludedSignals", max_length=10)
    unavailable_signals: list[str] = Field(alias="unavailableSignals", max_length=10)
    normal_sharing_possibilities: list[str] = Field(alias="normalSharingPossibilities", max_length=10)
    score_breakdown: MultiEventScoreBreakdown = Field(alias="scoreBreakdown")


class MultiEventCandidatePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUIDText = Field(alias="jobId")
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    cursor: str | None = Field(default=None, pattern=r"^\d+$")
    next_cursor: str | None = Field(default=None, alias="nextCursor", pattern=r"^\d+$")
    total_count: StrictInt = Field(alias="totalCount", ge=0, le=10_000)
    records: list[MultiEventCandidateSummary]


class MultiEventMemberPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUIDText = Field(alias="jobId")
    candidate_id: str = Field(alias="candidateId", min_length=1, max_length=128)
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    cursor: str | None = Field(default=None, pattern=r"^\d+$")
    next_cursor: str | None = Field(default=None, alias="nextCursor", pattern=r"^\d+$")
    total_count: StrictInt = Field(alias="totalCount", ge=0, le=50_000)
    records: list[HashToken]


class MultiEventRelationshipSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship_id: str = Field(alias="relationshipId", min_length=1, max_length=80)
    signal_type: str = Field(alias="signalType", min_length=1, max_length=64)
    account_count: StrictInt = Field(alias="accountCount", ge=2, le=50_000)
    source_scope_count: StrictInt = Field(alias="sourceScopeCount", ge=1, le=500)
    source_scopes: list[MultiEventLedgerScope] = Field(alias="sourceScopes", min_length=1, max_length=500)


class MultiEventRelationshipPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUIDText = Field(alias="jobId")
    candidate_id: str = Field(alias="candidateId", min_length=1, max_length=128)
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    cursor: str | None = Field(default=None, pattern=r"^\d+$")
    next_cursor: str | None = Field(default=None, alias="nextCursor", pattern=r"^\d+$")
    total_count: StrictInt = Field(alias="totalCount", ge=0, le=10_000)
    records: list[MultiEventRelationshipSummary]


class MultiEventRelationship(BaseModel):
    """Spring 결과 저장용 내부 근거입니다. 웹 응답에는 원본 HMAC을 노출하지 않습니다."""

    model_config = ConfigDict(extra="forbid")

    signal_type: str = Field(alias="signalType", min_length=1, max_length=64)
    account_ids: list[HashToken] = Field(alias="accountIds", min_length=2, max_length=50_000)
    source_scopes: list[MultiEventLedgerScope] = Field(alias="sourceScopes", min_length=1, max_length=500)


class MultiEventCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(alias="candidateId", min_length=1, max_length=128)
    priority_band: Literal["HIGH", "MEDIUM", "LOW"] = Field(alias="priorityBand")
    score_version: Literal["score-v2-exp2-multi-performance-recurrence"] = Field(alias="scoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    distinct_account_count: StrictInt = Field(alias="distinctAccountCount", ge=2)
    distinct_event_count: StrictInt = Field(alias="distinctEventCount", ge=1)
    distinct_performance_count: StrictInt = Field(alias="distinctPerformanceCount", ge=2)
    verified_relationship_count: StrictInt = Field(alias="verifiedRelationshipCount", ge=1)
    source_scopes: list[MultiEventLedgerScope] = Field(alias="sourceScopes", min_length=2)
    available_signals: list[str] = Field(alias="availableSignals", max_length=10)
    excluded_signals: list[str] = Field(alias="excludedSignals", max_length=10)
    unavailable_signals: list[str] = Field(alias="unavailableSignals", max_length=10)
    normal_sharing_possibilities: list[str] = Field(alias="normalSharingPossibilities", max_length=10)
    score_breakdown: MultiEventScoreBreakdown = Field(alias="scoreBreakdown")
    member_account_ids: list[HashToken] = Field(alias="memberAccountIds", min_length=2, max_length=50_000)
    relationships: list[MultiEventRelationship] = Field(alias="relationships", min_length=1, max_length=50_000)


class MultiEventAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: UUIDText = Field(alias="analysisId")
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    score_version: Literal["score-v2-exp2-multi-performance-recurrence"] = Field(alias="scoreVersion")
    projection_score_version: Literal["score-v2-exp2"] = Field(alias="projectionScoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    algorithm: Literal["GDS_LOUVAIN"] = Field(alias="algorithm")
    algorithm_version: ShortText = Field(alias="algorithmVersion")
    candidates: list[MultiEventCandidate] = Field(max_length=10_000)
