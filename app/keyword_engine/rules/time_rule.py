from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 영업시간·운영시간 문의가 주 목적인 발화
BUSINESS_HOURS = [
    "영업시간", "영업 시간", "운영시간", "운영 시간",
    "몇 시까지", "몇시까지", "언제까지 해요", "언제까지 하나요",
    "몇 시에 열어요", "몇시에 열어요", "몇 시에 닫아요", "몇시에 닫아요",
    "문 여는 시간", "문 닫는 시간", "오픈", "마감",
    "아직 해요", "아직 하나요", "지금 가능해요",
]

# 특정 시간대 방문/서비스 관련
TIME_REQUEST = [
    "시에 가도 돼요", "시에 가능해요", "시쯤", "시 괜찮아요",
    "몇 시", "몇시", "시간이 어떻게", "시간 돼요", "시간 되나요",
]

RESERVATION_OVERLAP = ["예약", "방문 예약"]


class TimeRule(BaseRule):
    intent = "time_inquiry"

    def match(self, text: str) -> Optional[EngineResult]:
        biz_hits = [kw for kw in BUSINESS_HOURS if kw in text]
        req_hits = [kw for kw in TIME_REQUEST if kw in text]
        all_hits = biz_hits + req_hits

        if not all_hits:
            return None

        slots = {}
        slots.update(sp.extract_time(text))
        slots.update(sp.extract_date(text))

        # 영업시간 키워드는 가중치 2배
        kw_weight = len(biz_hits) * 2 + len(req_hits)
        confidence = sp.score(kw_weight, len(slots))

        # 예약 맥락이 함께 있으면 reservation_rule에 우선권
        if any(kw in text for kw in RESERVATION_OVERLAP):
            confidence = round(confidence * 0.80, 4)

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=all_hits,
            extracted_slots=slots,
        )
