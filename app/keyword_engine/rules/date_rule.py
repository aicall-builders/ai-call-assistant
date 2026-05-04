from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 날짜 확인/변경/협의가 주 목적인 발화 키워드
KEYWORDS = [
    "언제", "날짜", "날짜가", "날짜를", "날짜 확인", "날짜 변경",
    "며칠", "몇 일", "몇일", "언제 가능", "언제 돼요", "언제 되나요",
    "일정", "일정 확인", "일정이", "스케줄",
    "다음 주", "이번 주", "이번 달", "다음 달",
    "오늘", "내일", "모레", "글피",
]

# 예약/배송 룰과 중복되므로, 단독으로 날짜 언급이 주 목적일 때만 반응
RESERVATION_OVERLAP = ["예약", "방문", "자리", "테이블", "성함", "명이요", "분이요"]
DELIVERY_OVERLAP = ["배송", "배달", "택배"]


class DateRule(BaseRule):
    intent = "date_inquiry"

    def match(self, text: str) -> Optional[EngineResult]:
        hits = [kw for kw in KEYWORDS if kw in text]
        # "3일 후", "2주 뒤" 등 DATE_LATER 패턴도 트리거로 허용
        if not hits and not sp.DATE_LATER.search(text):
            return None
        if not hits:
            hits = [sp.DATE_LATER.search(text).group(0)]

        # 예약/배송 전용 키워드가 있으면 해당 룰에 우선권을 넘김 (낮은 confidence)
        overlap = any(kw in text for kw in RESERVATION_OVERLAP + DELIVERY_OVERLAP)

        slots = {}
        slots.update(sp.extract_date(text))
        slots.update(sp.extract_time(text))

        confidence = sp.score(len(hits), len(slots))
        # 더 구체적인 룰이 있을 때는 confidence를 낮춰 해당 룰이 이김
        if overlap:
            confidence = round(confidence * 0.75, 4)

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=hits,
            extracted_slots=slots,
        )
