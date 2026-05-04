from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EngineResult:
    intent: Optional[str]
    confidence: float              # 0.0 ~ 1.0
    matched_keywords: list[str] = field(default_factory=list)
    extracted_slots: dict = field(default_factory=dict)
    needs_ai: bool = False
