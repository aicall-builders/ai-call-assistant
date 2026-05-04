from app.keyword_engine.rules.reservation_rule import ReservationRule
from app.keyword_engine.rules.delivery_rule import DeliveryRule
from app.keyword_engine.rules.amount_rule import AmountRule
from app.keyword_engine.rules.date_rule import DateRule
from app.keyword_engine.rules.time_rule import TimeRule
from app.keyword_engine.rules.todo_rule import TodoRule
from app.keyword_engine.rules.inquiry_rule import InquiryRule

# 우선순위 순서: 구체적(전용) → 범용
RULE_REGISTRY = [
    ReservationRule(),
    DeliveryRule(),
    AmountRule(),
    TodoRule(),
    TimeRule(),
    DateRule(),
    InquiryRule(),   # 범용 폴백 — 항상 마지막
]
