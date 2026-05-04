from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class STTResult:
    text: str
    confidence: float
    language: str = "ko"


class BaseSTTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_bytes: bytes) -> STTResult:
        ...
