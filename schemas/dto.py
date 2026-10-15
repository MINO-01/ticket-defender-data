from datetime import datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, WithJsonSchema, model_validator
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

# VLM 제보 파싱 DTO
class VLMParsedData(BaseModel):
    """VLM 환각 방지를 위한 엄격한 포맷"""
    zone: Optional[str] = Field(None, description="구역")
    row: Optional[str] = Field(None, description="열")
    seat: Optional[str] = Field(None, description="좌석 번호")

class VLMResponse(BaseModel):
    status: str = Field(default="success")
    message: str
    parsed_data: VLMParsedData
