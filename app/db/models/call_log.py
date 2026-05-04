from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Boolean, JSON
from app.db.base import Base


class CallLog(Base):
    __tablename__ = "call_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # STT
    transcript = Column(Text, nullable=False)
    stt_confidence = Column(Float)
    audio_duration_sec = Column(Float)

    # 키워드 엔진
    intent = Column(String(64))
    keyword_confidence = Column(Float)
    matched_keywords = Column(JSON)
    extracted_slots = Column(JSON)

    # AI 호출 여부 및 결과
    ai_called = Column(Boolean, default=False)
    ai_result = Column(JSON)

    # 최종 응답
    response_text = Column(Text)
