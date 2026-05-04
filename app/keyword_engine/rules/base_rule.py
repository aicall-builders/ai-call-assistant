from abc import ABC, abstractmethod
from typing import Optional
from app.keyword_engine.result import EngineResult


class BaseRule(ABC):
    intent: str

    @abstractmethod
    def match(self, text: str) -> Optional[EngineResult]:
        """텍스트가 이 룰에 매칭되면 EngineResult, 아니면 None."""
        ...
