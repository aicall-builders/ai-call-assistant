import io
import whisper
import numpy as np
import soundfile as sf
from app.stt.base import BaseSTTProvider, STTResult


class WhisperSTTProvider(BaseSTTProvider):
    def __init__(self, model_size: str = "base"):
        self._model = whisper.load_model(model_size)

    async def transcribe(self, audio_bytes: bytes) -> STTResult:
        audio_array, _ = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        result = self._model.transcribe(audio_array, language="ko", fp16=False)
        return STTResult(
            text=result["text"].strip(),
            confidence=1.0,   # Whisper는 세그먼트 평균으로 추후 개선 가능
            language=result.get("language", "ko"),
        )
