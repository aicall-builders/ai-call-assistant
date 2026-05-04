from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 예약 의도를 강하게 나타내는 키워드
PRIMARY = ["예약", "예약하고 싶", "예약할게요", "예약해주세요", "예약 가능"]
# 예약 맥락에서 나타나는 보조 키워드
SECONDARY = ["자리", "테이블", "좌석", "방", "룸", "자리 있", "빈자리",
             "방문", "방문할게요", "신청", "접수", "잡아주세요",
             "성함", "이름으로", "명이요", "분이요"]
# 취소/변경은 별도 인텐트 처리
NEGATIVE = ["취소", "환불", "변경", "바꾸고"]


class ReservationRule(BaseRule):
    intent = "reservation"

    def match(self, text: str) -> Optional[EngineResult]:
        # 취소/변경 발화는 이 룰에서 제외
        if any(kw in text for kw in NEGATIVE):
            return None

        primary_hits = [kw for kw in PRIMARY if kw in text]
        secondary_hits = [kw for kw in SECONDARY if kw in text]
        all_hits = primary_hits + secondary_hits

        if not all_hits:
            return None

        slots = {}
        slots.update(sp.extract_date(text))
        slots.update(sp.extract_time(text))
        slots.update(sp.extract_people(text))
        slots.update(sp.extract_name(text))
        slots.update(sp.extract_phone(text))

        # 보조 키워드만 있으면 primary 보정 없이 낮은 베이스
        kw_weight = len(primary_hits) * 2 + len(secondary_hits)
        confidence = sp.score(kw_weight, len(slots))

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=all_hits,
            extracted_slots=slots,
        )
