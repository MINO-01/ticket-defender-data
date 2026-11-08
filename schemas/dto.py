from datetime import datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints, WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError

# 매크로 조직망 탐지 DTO
class TicketHashData(BaseModel):
    account_id: str = Field(..., alias="accountId")     # 예매처 내부 식별자 (UID)
    payment_hash: str = Field(..., alias="paymentHash") # 결제수단 해시
    address_hash: str = Field(..., alias="addressHash") # 배송지 해시
    device_id_hash: str = Field(..., alias="deviceIdHash") # 기기 고유 식별자 해시
    ip_hash: str = Field(..., alias="ipHash")           # 접속 IP 해시

class AnalysisRequest(BaseModel):
    tickets: List[TicketHashData]

class MacroClusterData(BaseModel):
    cluster_id: int = Field(..., description="Neo4j WCC 군집 ID")
    account_count: int = Field(..., description="조직망에 엮인 예매 계정 수")
    accounts: List[str] = Field(..., description="계정(Account ID) 목록")

class ClusterResponse(BaseModel):
    """API 공통 응답 규격"""
    status: str = Field(default="success")
    message: str
    data: List[MacroClusterData]


HashToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TicketIdentifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]


def validate_uuid_text(value: str) -> str:
    try:
        UUID(value)
    except ValueError:
        raise PydanticCustomError("invalid_uuid", "Invalid UUID") from None
    return value


UUIDText = Annotated[
    str, AfterValidator(validate_uuid_text), WithJsonSchema({"type": "string", "format": "uuid"})
]


class Track1TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: TicketIdentifier = Field(alias="accountId")
    reservation_no: TicketIdentifier = Field(alias="reservationNo")
    event_id: TicketIdentifier = Field(alias="eventId")
    payment_hash: Optional[HashToken] = Field(default=None, alias="paymentHash")
    address_hash: Optional[HashToken] = Field(default=None, alias="addressHash")
    device_id_hash: Optional[HashToken] = Field(default=None, alias="deviceIdHash")
    ip_hash: Optional[HashToken] = Field(default=None, alias="ipHash")

    @model_validator(mode="after")
    def validate_ticket(self):
        if not all((self.account_id.strip(), self.reservation_no.strip(), self.event_id.strip())):
            raise PydanticCustomError("invalid_identifier", "Ticket identifiers must not be blank")
        if not any((self.payment_hash, self.address_hash, self.device_id_hash, self.ip_hash)):
            raise PydanticCustomError("missing_connection_token", "At least one connection token is required")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return self.account_id, self.reservation_no, self.event_id

    @property
    def analysis_scope(self) -> str:
        return self.event_id


class Track1AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUIDText = Field(alias="requestId")
    tickets: List[Track1TicketInput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_tickets(self):
        keys = [ticket.key for ticket in self.tickets]
        if len(keys) != len(set(keys)):
            raise PydanticCustomError("duplicate_ticket", "Duplicate ticket identifiers are not allowed")
        return self


class Track1ClusterMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: TicketIdentifier = Field(alias="accountId")
    reservation_no: TicketIdentifier = Field(alias="reservationNo")
    event_id: TicketIdentifier = Field(alias="eventId")


class Track1Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str = Field(min_length=1, alias="clusterId")
    members: List[Track1ClusterMember] = Field(min_length=2)
    payment_hashes: List[HashToken] = Field(alias="paymentHashes")
    address_hashes: List[HashToken] = Field(alias="addressHashes")
    device_id_hashes: List[HashToken] = Field(alias="deviceIdHashes")
    ip_hashes: List[HashToken] = Field(alias="ipHashes")
    risk_score: float = Field(ge=0, le=1, allow_inf_nan=False, alias="riskScore")


class Track1AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUIDText = Field(alias="requestId")
    status: Literal["COMPLETED"]
    algorithm: Literal["LOUVAIN"]
    algorithm_version: str = Field(min_length=1, alias="algorithmVersion")
    analyzed_at: datetime = Field(alias="analyzedAt")
    clusters: List[Track1Cluster]


# Backend docs/api/track1-purchase-limit-analysis-v2.yaml 계약의 DTO입니다.
# 계약 상태는 PROPOSED_FOR_CROSS_REPOSITORY_REVIEW이며 Spring 연동 검증 전입니다.
PolicyVersion = Annotated[str, StringConstraints(min_length=1, max_length=128)]
PositiveQuantity = Annotated[StrictInt, Field(ge=1)]


class Track1ConfirmedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tickets_per_account: PositiveQuantity = Field(alias="maxTicketsPerAccount")
    policy_version: PolicyVersion = Field(alias="policyVersion")
    effective_from: AwareDatetime = Field(alias="effectiveFrom")
    effective_until: Optional[AwareDatetime] = Field(default=None, alias="effectiveUntil")

    @model_validator(mode="after")
    def validate_policy(self):
        if not self.policy_version.strip():
            raise PydanticCustomError("INVALID_POLICY", "policyVersion must not be blank")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise PydanticCustomError("POLICY_TIME_INVALID", "effectiveUntil must be after effectiveFrom")
        return self

    def applies_at(self, moment: datetime) -> bool:
        """시작 시각은 포함하고 종료 시각은 제외한 구간으로 판단합니다."""

        return self.effective_from <= moment and (self.effective_until is None or moment < self.effective_until)


class Track1PerformanceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_event_id: UUIDText = Field(alias="internalEventId")
    event_id: TicketIdentifier = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    policy_verification_status: Literal["CONFIRMED", "POLICY_NOT_CONFIRMED"] = Field(alias="policyVerificationStatus")
    policy: Optional[Track1ConfirmedPolicy] = None
    quantity_data_status: Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"] = Field(alias="quantityDataStatus")
    quantity_source_version: Optional[Annotated[str, StringConstraints(max_length=128)]] = Field(
        default=None, alias="quantitySourceVersion"
    )
    quantity_as_of: Optional[AwareDatetime] = Field(default=None, alias="quantityAsOf")
    cancellation_handling: Literal["NETTED", "NOT_CONFIRMED"] = Field(alias="cancellationHandling")
    duplicate_handling: Literal["DEDUPLICATED", "NOT_CONFIRMED"] = Field(alias="duplicateHandling")
    data_completeness_reason: Optional[Annotated[str, StringConstraints(max_length=512)]] = Field(
        default=None, alias="dataCompletenessReason"
    )

    @model_validator(mode="after")
    def validate_policy_status(self):
        if (self.policy_verification_status == "CONFIRMED") != (self.policy is not None):
            raise PydanticCustomError("INVALID_POLICY", "policy must match policyVerificationStatus")
        return self

    @property
    def quantity_is_complete(self) -> bool:
        """수량 출처와 기준 시각까지 확인된 경우에만 완전한 수량으로 봅니다."""

        return (
            self.quantity_data_status == "COMPLETE"
            and self.cancellation_handling == "NETTED"
            and self.duplicate_handling == "DEDUPLICATED"
            and self.quantity_source_version is not None
            and bool(self.quantity_source_version.strip())
            and self.quantity_as_of is not None
        )


class Track1LimitTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: TicketIdentifier = Field(alias="accountId")
    reservation_no: TicketIdentifier = Field(alias="reservationNo")
    event_id: TicketIdentifier = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    purchased_at: AwareDatetime = Field(alias="purchasedAt")
    effective_ticket_quantity: PositiveQuantity = Field(alias="effectiveTicketQuantity")
    payment_hash: Optional[HashToken] = Field(default=None, alias="paymentHash")
    address_hash: Optional[HashToken] = Field(default=None, alias="addressHash")
    device_id_hash: Optional[HashToken] = Field(default=None, alias="deviceIdHash")
    ip_hash: Optional[HashToken] = Field(default=None, alias="ipHash")

    @model_validator(mode="after")
    def validate_ticket(self):
        if not all((self.account_id.strip(), self.reservation_no.strip(), self.event_id.strip())):
            raise PydanticCustomError("invalid_identifier", "Ticket identifiers must not be blank")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return self.account_id, self.reservation_no, self.event_id

    @property
    def analysis_scope(self) -> str:
        return self.performance_id.lower()


class Track1LimitAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUIDText = Field(alias="requestId")
    performance_scopes: List[Track1PerformanceScope] = Field(min_length=1, alias="performanceScopes")
    tickets: List[Track1LimitTicketInput] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scopes(self):
        scope_events = {}
        for scope in self.performance_scopes:
            performance_id = scope.performance_id.lower()
            if performance_id in scope_events:
                raise PydanticCustomError("DUPLICATE_PERFORMANCE_SCOPE", "performance scope must be unique")
            scope_events[performance_id] = scope.event_id
        for ticket in self.tickets:
            if scope_events.get(ticket.analysis_scope) != ticket.event_id:
                raise PydanticCustomError("TICKET_SCOPE_MISMATCH", "ticket must match one performance scope")
        keys = [ticket.key for ticket in self.tickets]
        if len(keys) != len(set(keys)):
            raise PydanticCustomError("duplicate_ticket", "Duplicate ticket identifiers are not allowed")
        return self


class Track1ReservationQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reservation_no: TicketIdentifier = Field(alias="reservationNo")
    effective_ticket_quantity: PositiveQuantity = Field(alias="effectiveTicketQuantity")


class Track1AccountLimitFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: TicketIdentifier = Field(alias="accountId")
    effective_ticket_quantity: PositiveQuantity = Field(alias="effectiveTicketQuantity")
    max_tickets_per_account: PositiveQuantity = Field(alias="maxTicketsPerAccount")
    policy_version: PolicyVersion = Field(alias="policyVersion")
    reservation_quantities: List[Track1ReservationQuantity] = Field(min_length=1, alias="reservationQuantities")


class Track1PurchaseLimitEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_event_id: UUIDText = Field(alias="internalEventId")
    event_id: TicketIdentifier = Field(alias="eventId")
    performance_id: UUIDText = Field(alias="performanceId")
    status: Literal["EVALUATED", "POLICY_NOT_CONFIRMED", "QUANTITY_DATA_INCOMPLETE", "POLICY_TIME_NOT_APPLICABLE"]
    policy_version: Optional[PolicyVersion] = Field(alias="policyVersion")
    account_limit_findings: List[Track1AccountLimitFinding] = Field(alias="accountLimitFindings")


class Track1LimitAnalysisResponse(Track1AnalysisResponse):
    purchase_limit_evaluations: List[Track1PurchaseLimitEvaluation] = Field(alias="purchaseLimitEvaluations")


# VLM 제보 파싱 DTO
class VLMParsedData(BaseModel):
    """Legacy seat response shape with bounded model-provided strings."""
    model_config = ConfigDict(extra="forbid", strict=True)

    zone: Optional[str] = Field(None, min_length=1, max_length=64, description="구역")
    row: Optional[str] = Field(None, min_length=1, max_length=64, description="열")
    seat: Optional[str] = Field(None, min_length=1, max_length=64, description="좌석 번호")

class VLMResponse(BaseModel):
    status: str = Field(default="success")
    message: str
    parsed_data: VLMParsedData
