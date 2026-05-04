from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 가격 문의 키워드
PRICE_INQUIRY = [
    "가격", "가격이", "얼마예요", "얼마에요", "얼마나 해요", "얼마나 돼요",
    "비용", "비용이", "요금", "요금이", "수수료",
    "얼마짜리", "몇 만 원", "몇만원",
]

# 결제 관련 키워드
PAYMENT = [
    "결제", "계산", "카드", "현금", "계좌이체", "이체", "입금",
    "카드 돼요", "카드 되나요", "현금 영수증", "영수증",
    "어떻게 결제", "결제 방법",
]

# 할인/환불 관련 키워드
DISCOUNT_REFUND = [
    "할인", "할인해주세요", "깎아주세요", "싸게", "더 싸게",
    "환불", "환불해주세요", "돌려주세요", "반품",
    "쿠폰", "포인트", "적립",
]

# 견적 관련
QUOTE = ["견적", "견적서", "예상 금액", "예상 비용", "총 얼마"]


class AmountRule(BaseRule):
    intent = "amount"

    def match(self, text: str) -> Optional[EngineResult]:
        price_hits = [kw for kw in PRICE_INQUIRY if kw in text]
        payment_hits = [kw for kw in PAYMENT if kw in text]
        discount_hits = [kw for kw in DISCOUNT_REFUND if kw in text]
        quote_hits = [kw for kw in QUOTE if kw in text]
        all_hits = price_hits + payment_hits + discount_hits + quote_hits

        if not all_hits:
            return None

        slots = {}
        slots.update(sp.extract_amount(text))

        # 서브 인텐트 분류
        if discount_hits and "환불" in text or "반품" in text:
            slots["sub_intent"] = "refund"
        elif discount_hits:
            slots["sub_intent"] = "discount"
        elif payment_hits:
            slots["sub_intent"] = "payment"
        elif quote_hits:
            slots["sub_intent"] = "quote"
        else:
            slots["sub_intent"] = "inquiry"

        # 가격 문의 + 금액 명시는 더 구체적인 발화
        kw_weight = len(price_hits) * 2 + len(payment_hits) + len(discount_hits) + len(quote_hits) * 2
        confidence = sp.score(kw_weight, len(slots))

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=all_hits,
            extracted_slots=slots,
        )
