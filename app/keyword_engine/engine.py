from typing import Optional
from app.keyword_engine.result import EngineResult


class KeywordEngine:
    """룰 기반 인텐트 분류 + 슬롯 추출. confidence < threshold 이면 AI로 위임."""

    def __init__(self, ai_fallback_threshold: float = 0.4):
        self.threshold = ai_fallback_threshold
        # 순환 임포트 방지를 위해 지연 로딩
        self._rules = None

    def _get_rules(self):
        if self._rules is None:
            from app.keyword_engine.rules.registry import RULE_REGISTRY
            self._rules = RULE_REGISTRY
        return self._rules

    def process(self, text: str) -> EngineResult:
        best: Optional[EngineResult] = None

        for rule in self._get_rules():
            result = rule.match(text)
            if result and (best is None or result.confidence > best.confidence):
                best = result

        if best is None or best.confidence < self.threshold:
            return EngineResult(
                intent=best.intent if best else None,
                confidence=best.confidence if best else 0.0,
                needs_ai=True,
            )

        return best
