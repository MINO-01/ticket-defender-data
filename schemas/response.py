from pydantic import BaseModel, Field
from typing import List

class ClusterData(BaseModel):
    """단일 암표 의심 군집 데이터 규격"""
    payment_hash: str = Field(..., description="비식별화된 결제 수단 해시값")
    account_count: int = Field(..., description="해당 결제 수단에 연결된 예매 계정 수")
    accounts: List[str] = Field(..., description="연결된 예매 계정(Account ID) 목록")

class ClusterResponse(BaseModel):
    """API 공통 응답 규격"""
    status: str = Field(default="success")
    message: str
    data: List[ClusterData]