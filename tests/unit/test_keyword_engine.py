"""소상공인 한국어 통화 시나리오 기반 키워드 엔진 단위 테스트."""
import pytest
from app.keyword_engine.engine import KeywordEngine

engine = KeywordEngine(ai_fallback_threshold=0.4)


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────
def process(text: str):
    return engine.process(text)


# ── 예약 (reservation) ────────────────────────────────────────────────────────
class TestReservation:
    def test_basic(self):
        r = process("내일 저녁 7시에 4명 예약하고 싶어요")
        assert r.intent == "reservation"
        assert r.confidence >= 0.7
        assert r.extracted_slots.get("people") == 4
        assert "저녁" in r.extracted_slots.get("time", "")

    def test_with_name(self):
        r = process("이번 주 토요일 오후 1시에 두 명이요. 성함은 김민준으로 해주세요")
        assert r.intent == "reservation"
        assert r.extracted_slots.get("people") == 2
        assert r.extracted_slots.get("name") == "김민준"

    def test_table_keyword(self):
        r = process("지금 자리 있어요? 세 명이요")
        assert r.intent == "reservation"
        assert r.extracted_slots.get("people") == 3

    def test_cancellation_excluded(self):
        r = process("예약 취소하고 싶어요")
        assert r.intent != "reservation" or r.confidence < 0.4

    def test_no_reservation(self):
        r = process("안녕하세요 메뉴가 궁금해서요")
        assert r.intent != "reservation"


# ── 날짜 (date_inquiry) ───────────────────────────────────────────────────────
class TestDate:
    def test_absolute_date(self):
        r = process("5월 3일에 방문 가능한가요")
        assert r.extracted_slots.get("date") is not None

    def test_relative_date(self):
        r = process("다음 주 언제가 가능한지 확인하고 싶어요")
        assert r.intent == "date_inquiry"
        assert "다음 주" in r.extracted_slots.get("date", "")

    def test_weekday(self):
        r = process("이번 주 수요일 일정 확인하고 싶어요")
        assert "수요일" in r.extracted_slots.get("date", "")

    def test_n_days_later(self):
        r = process("3일 후에 가능한가요")
        assert r.intent == "date_inquiry"
        assert r.extracted_slots.get("date") is not None

    def test_reservation_overlap_lowers_confidence(self):
        r_date = process("다음 주에 예약 가능한가요")
        # 예약 룰이 이겨야 함
        assert r_date.intent == "reservation"


# ── 시간 (time_inquiry) ───────────────────────────────────────────────────────
class TestTime:
    def test_business_hours(self):
        r = process("영업시간이 어떻게 되나요")
        assert r.intent == "time_inquiry"
        assert r.confidence >= 0.6

    def test_closing_time(self):
        r = process("몇 시까지 하나요")
        assert r.intent == "time_inquiry"

    def test_specific_time(self):
        r = process("오후 3시에 가도 돼요?")
        assert r.intent == "time_inquiry"
        assert "오후" in r.extracted_slots.get("time", "")

    def test_open_question(self):
        r = process("지금 문 열었어요? 아직 하나요")
        assert r.intent == "time_inquiry"


# ── 금액 (amount) ─────────────────────────────────────────────────────────────
class TestAmount:
    def test_price_inquiry(self):
        r = process("1인분에 얼마예요")
        assert r.intent == "amount"
        assert r.extracted_slots.get("sub_intent") == "inquiry"

    def test_discount_request(self):
        r = process("좀 깎아주세요. 5만원은 너무 비싼데")
        assert r.intent == "amount"
        assert r.extracted_slots.get("sub_intent") == "discount"
        assert "5만원" in r.extracted_slots.get("amount", "")

    def test_refund(self):
        r = process("환불해주세요 카드로 결제했어요")
        assert r.intent == "amount"
        assert r.extracted_slots.get("sub_intent") == "refund"

    def test_payment_method(self):
        r = process("카드 결제 되나요")
        assert r.intent == "amount"
        assert r.extracted_slots.get("sub_intent") == "payment"

    def test_amount_extraction(self):
        r = process("견적이 총 얼마인지 알고 싶어요")
        assert r.intent == "amount"


# ── 배송 (delivery) ───────────────────────────────────────────────────────────
class TestDelivery:
    def test_order(self):
        r = process("치킨 두 마리 배달 시키고 싶어요")
        assert r.intent == "delivery"
        assert r.extracted_slots.get("sub_intent") == "order"

    def test_tracking(self):
        r = process("주문한 물건 언제 와요? 아직 안 왔어요")
        assert r.intent == "delivery"
        assert r.extracted_slots.get("sub_intent") == "tracking"

    def test_delivery_time(self):
        r = process("오늘 오후 6시에 배달 가능한가요")
        assert r.intent == "delivery"
        assert r.extracted_slots.get("date") is not None
        assert r.extracted_slots.get("time") is not None

    def test_quantity(self):
        r = process("김치 5박스 택배로 보내주세요")
        assert r.intent == "delivery"
        assert r.extracted_slots.get("quantity") == "5박스"

    def test_address(self):
        r = process("배달 주소가 어디예요? 어디로 오나요")
        assert r.intent == "delivery"


# ── 할일 (todo) ───────────────────────────────────────────────────────────────
class TestTodo:
    def test_callback_request(self):
        r = process("사장님 나중에 다시 전화해주세요")
        assert r.intent == "todo"
        assert r.extracted_slots.get("sub_intent") == "callback"

    def test_memo_request(self):
        r = process("메모 좀 해주세요. 내일 오후 2시에 방문한다고요")
        assert r.intent == "todo"
        assert r.extracted_slots.get("sub_intent") == "memo"

    def test_message_delivery(self):
        r = process("담당자한테 전해주세요. 오늘 못 간다고요")
        assert r.intent == "todo"
        assert r.extracted_slots.get("sub_intent") == "callback"

    def test_with_phone(self):
        r = process("010-1234-5678로 다시 연락 주세요")
        assert r.intent == "todo"
        assert r.extracted_slots.get("phone") == "010-1234-5678"


# ── AI 폴백 ───────────────────────────────────────────────────────────────────
class TestAIFallback:
    def test_unrecognized_intent(self):
        r = process("저 지난번에 뭔가 얘기했는데 기억하시나요")
        assert r.needs_ai is True

    def test_empty_text(self):
        r = process("")
        assert r.needs_ai is True
        assert r.confidence == 0.0


# ── 슬롯 패턴 직접 테스트 ──────────────────────────────────────────────────────
class TestSlotPatterns:
    def test_absolute_date(self):
        from app.keyword_engine.rules.slot_patterns import extract_date
        assert extract_date("4월 27일에 방문")["date"] == "4월 27일"

    def test_relative_date(self):
        from app.keyword_engine.rules.slot_patterns import extract_date
        assert extract_date("모레 가능해요")["date"] == "모레"

    def test_time_with_period(self):
        from app.keyword_engine.rules.slot_patterns import extract_time
        result = extract_time("오후 2시 30분에 오세요")
        assert "오후" in result["time"]
        assert "2시" in result["time"]

    def test_amount_manwon(self):
        from app.keyword_engine.rules.slot_patterns import extract_amount
        assert "10만원" in extract_amount("10만원짜리 세트")["amount"]

    def test_people_kor(self):
        from app.keyword_engine.rules.slot_patterns import extract_people
        assert extract_people("세 명이요")["people"] == 3

    def test_phone_extraction(self):
        from app.keyword_engine.rules.slot_patterns import extract_phone
        assert extract_phone("010-9876-5432로 연락주세요")["phone"] == "010-9876-5432"

    def test_name_extraction(self):
        from app.keyword_engine.rules.slot_patterns import extract_name
        assert extract_name("성함은 홍길동으로 해주세요")["name"] == "홍길동"
