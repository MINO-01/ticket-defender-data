from pydantic import BaseModel, Field
from typing import List, Optional

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