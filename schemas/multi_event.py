"""다공연 조사 분석의 내부 계약입니다.

이 계약은 Spring의 인증된 호출만 받습니다. tenant 값은 사용자 입력으로 신뢰하지 않으며,
원본 식별자나 원본 IP를 받지 않습니다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from schemas.dto import HashToken, IpNetworkType, UUIDText


ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=512)]


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


class MultiEventAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: UUIDText = Field(alias="analysisId")
    tenant_id: UUIDText = Field(alias="tenantId")
    ledger_version_set_digest: HashToken = Field(alias="ledgerVersionSetDigest")
    analysis_as_of: AwareDatetime = Field(alias="analysisAsOf")
    analysis_start_at: AwareDatetime = Field(alias="analysisStartAt")
    analysis_end_at: AwareDatetime = Field(alias="analysisEndAt")
    score_version: Literal["score-v2-exp2-multi-event-recurrence"] = Field(alias="scoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    ledger_scopes: list[MultiEventLedgerScope] = Field(alias="ledgerScopes", min_length=2, max_length=500)
    tickets: list[MultiEventTicketInput] = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def validate_fixed_scope(self):
        if self.analysis_end_at <= self.analysis_start_at or self.analysis_end_at > self.analysis_as_of:
            raise PydanticCustomError("invalid_analysis_period", "Analysis period must be ordered and not after analysisAsOf")
        scope_keys = [scope.key for scope in self.ledger_scopes]
        if len(scope_keys) != len(set(scope_keys)):
            raise PydanticCustomError("duplicate_ledger_scope", "Ledger scopes must be unique")
        scope_set = set(scope_keys)
        scopes_by_key = {scope.key: scope for scope in self.ledger_scopes}
        if any(ticket.scope_key not in scope_set for ticket in self.tickets):
            raise PydanticCustomError("ticket_scope_mismatch", "Ticket scope must be fixed by ledgerScopes")
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
        suppliers = {ticket.supplier_namespace for ticket in self.tickets}
        key_versions = {ticket.hmac_key_version for ticket in self.tickets}
        purposes = {ticket.hmac_purpose for ticket in self.tickets}
        if len(suppliers) != 1 or len(key_versions) != 1 or purposes != {"TRACK1_CONNECTION"}:
            raise PydanticCustomError("identity_contract_mixed", "Supplier, purpose, and key version must be uniform")
        return self


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
    score_version: Literal["score-v2-exp2-multi-event-recurrence"] = Field(alias="scoreVersion")
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
    score_version: Literal["score-v2-exp2-multi-event-recurrence"] = Field(alias="scoreVersion")
    projection_score_version: Literal["score-v2-exp2"] = Field(alias="projectionScoreVersion")
    projection_version: Literal["projection-v2.2-exp2-ipwindow-softhub"] = Field(alias="projectionVersion")
    algorithm: Literal["GDS_LOUVAIN"] = Field(alias="algorithm")
    algorithm_version: ShortText = Field(alias="algorithmVersion")
    candidates: list[MultiEventCandidate] = Field(max_length=10_000)
