from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ProcessTextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000, examples=["내일 저녁 7시에 2명 예약하고 싶어요"])
    business_context: str = Field(default="", examples=["한식 레스토랑, 영업시간 11시~22시, 최대 20명"])


class CallLogResponse(BaseModel):
    id: int
    created_at: datetime
    transcript: str
    intent: Optional[str]
    keyword_confidence: Optional[float]
    matched_keywords: Optional[list]
    extracted_slots: Optional[dict]
    ai_called: bool
    ai_result: Optional[dict]
    response_text: Optional[str]

    model_config = {"from_attributes": True}
