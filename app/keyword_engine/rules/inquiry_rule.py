from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 가게/서비스 일반 정보 문의 (다른 룰에서 처리 안 되는 범용 문의)
LOCATION = [
    "위치", "어디 있어요", "어디에 있어요", "어디예요", "주소가",
    "찾아가려고", "오는 방법", "네비", "주차",
]

MENU_PRODUCT = [
    "메뉴", "메뉴가", "뭐 있어요", "뭐 팔아요", "어떤 거",
    "취급", "판매", "종류", "어떤 메뉴",
]

GENERAL = [
    "휴일", "쉬는 날", "공휴일", "정기 휴일", "정기휴일",
    "예약 없이", "바로 가도 돼요", "워크인",
    "몇 명까지", "최대 인원", "단체",
    "반려동물", "펫", "주차 가능",
]


class InquiryRule(BaseRule):
    intent = "inquiry"

    def match(self, text: str) -> Optional[EngineResult]:
        loc_hits = [kw for kw in LOCATION if kw in text]
        menu_hits = [kw for kw in MENU_PRODUCT if kw in text]
        gen_hits = [kw for kw in GENERAL if kw in text]
        all_hits = loc_hits + menu_hits + gen_hits

        if not all_hits:
            return None

        slots = {}
        if loc_hits:
            slots["sub_intent"] = "location"
        elif menu_hits:
            slots["sub_intent"] = "menu"
        else:
            slots["sub_intent"] = "general"

        # 범용 룰이므로 다른 전용 룰보다 기본 confidence를 낮게 유지
        kw_weight = len(all_hits)
        confidence = sp.score(kw_weight, len(slots))
        confidence = round(confidence * 0.90, 4)

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=all_hits,
            extracted_slots=slots,
        )
