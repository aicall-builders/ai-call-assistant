import re
from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 배달/배송 핵심 키워드
PRIMARY = [
    "배달", "배송", "택배", "퀵", "퀵 서비스",
    "가져다줘요", "가져다 주세요", "갖다주세요",
    "배달 시켜요", "주문할게요", "주문하고 싶어요",
]

# 배송 상태 조회
TRACKING = [
    "언제 와요", "언제 도착", "도착했나요", "도착했어요",
    "아직 안 왔어요", "아직 안 왔는데", "배송 조회", "운송장",
    "오늘 오나요", "오늘 오는지", "오늘 받을 수 있어요",
    "배송 중", "출발했나요", "출발했어요",
]

# 주소/위치 관련
ADDRESS = [
    "주소", "어디로", "어디로 오나요", "어디서", "위치",
    "몇 층", "건물 이름", "아파트", "지번",
]

# 수량 패턴 (배달 수량)
QTY_PATTERN = re.compile(r"(\d+)\s*(개|박스|팩|봉|병|캔|통|장|벌|켤레|세트)")


class DeliveryRule(BaseRule):
    intent = "delivery"

    def match(self, text: str) -> Optional[EngineResult]:
        primary_hits = [kw for kw in PRIMARY if kw in text]
        tracking_hits = [kw for kw in TRACKING if kw in text]
        address_hits = [kw for kw in ADDRESS if kw in text]
        all_hits = primary_hits + tracking_hits + address_hits

        if not all_hits:
            return None

        slots = {}
        slots.update(sp.extract_date(text))
        slots.update(sp.extract_time(text))
        slots.update(sp.extract_amount(text))
        slots.update(sp.extract_phone(text))

        # 수량 추출
        qty_m = QTY_PATTERN.search(text)
        if qty_m:
            slots["quantity"] = f"{qty_m.group(1)}{qty_m.group(2)}"

        # 서브 인텐트
        if tracking_hits:
            slots["sub_intent"] = "tracking"
        elif address_hits and not primary_hits:
            slots["sub_intent"] = "address_inquiry"
        else:
            slots["sub_intent"] = "order"

        kw_weight = len(primary_hits) * 2 + len(tracking_hits) + len(address_hits)
        confidence = sp.score(kw_weight, len(slots))

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=all_hits,
            extracted_slots=slots,
        )
