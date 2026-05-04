from sqlalchemy.orm import Session
from app.keyword_engine.engine import KeywordEngine
from app.keyword_engine.result import EngineResult
from app.ai_handler.claude_handler import ClaudeHandler
from app.db.models.call_log import CallLog
from app.core.config import settings

_keyword_engine = KeywordEngine(ai_fallback_threshold=settings.ai_fallback_threshold)
_ai_handler = ClaudeHandler()


async def _run_pipeline(
    transcript: str,
    db: Session,
    business_context: str = "",
    stt_confidence: float | None = None,
    skip_ai: bool = False,
) -> CallLog:
    """STT 이후 공통 파이프라인: 키워드 엔진 → 필요시 AI → DB 저장."""
    engine_result: EngineResult = _keyword_engine.process(transcript)

    ai_called = False
    ai_result = None

    if engine_result.needs_ai and not skip_ai:
        ai_result = await _ai_handler.analyze(transcript, business_context)
        ai_called = True
        intent = ai_result.get("intent")
    else:
        intent = engine_result.intent

    log = CallLog(
        transcript=transcript,
        stt_confidence=stt_confidence,
        intent=intent,
        keyword_confidence=engine_result.confidence,
        matched_keywords=engine_result.matched_keywords,
        extracted_slots=engine_result.extracted_slots,
        ai_called=ai_called,
        ai_result=ai_result,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


async def process_call(audio_bytes: bytes, db: Session, business_context: str = "") -> CallLog:
    from app.stt.whisper_provider import WhisperSTTProvider
    stt = WhisperSTTProvider(settings.stt_model)
    stt_result = await stt.transcribe(audio_bytes)
    return await _run_pipeline(stt_result.text, db, business_context, stt_result.confidence)


async def process_text(transcript: str, db: Session, business_context: str = "") -> CallLog:
    """STT 없이 텍스트를 직접 파이프라인에 주입. AI 호출 없이 룰 엔진 결과만 반환."""
    return await _run_pipeline(transcript, db, business_context, stt_confidence=None, skip_ai=True)
