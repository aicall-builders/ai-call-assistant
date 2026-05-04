import re
from typing import Optional
from app.keyword_engine.rules.base_rule import BaseRule
from app.keyword_engine.result import EngineResult
from app.keyword_engine.rules import slot_patterns as sp

# 콜백/재연락 요청
CALLBACK = [
    "다시 전화해주세요", "다시 연락해주세요", "전화 주세요", "연락 주세요",
    "전화해주실 수 있나요", "연락해주실 수 있나요",
    "나중에 전화", "나중에 연락", "이따 전화", "이따 연락",
    "사장님한테 전해주세요", "사장님께 전해주세요",
    "담당자한테", "담당자에게",
]

# 메모/메시지 전달 요청
MEMO = [
    "메모", "메모해주세요", "메모 좀", "적어주세요", "남겨주세요",
    "전달해주세요", "전해주세요", "말씀 좀 전해주세요",
    "기록", "기록해주세요",
]

# 처리/작업 요청
ACTION = [
    "해주실 수 있나요", "부탁드려요", "부탁드립니다",
    "준비해주세요", "확인해주세요", "챙겨주세요",
    "신청해주세요", "등록해주세요", "취소해주세요",
]

# 일정 잡기 요청 (예약과 다름 - to-do 성격)
SCHEDULE = [
    "일정 잡아", "일정 잡아주세요", "스케줄 잡아",
    "미팅", "미팅 잡아", "상담 잡아", "상담 예약",
    "방문 일정", "방문 예약",
]

# 메시지 내용 추출 (큰따옴표 또는 "~라고" 패턴)
MESSAGE_PATTERN = re.compile(r'[\"“”](.+?)[\"“”]|(.+?)라고\s*(전해|말씀)')


class TodoRule(BaseRule):
    intent = "todo"

    def match(self, text: str) -> Optional[EngineResult]:
        cb_hits = [kw for kw in CALLBACK if kw in text]
        memo_hits = [kw for kw in MEMO if kw in text]
        action_hits = [kw for kw in ACTION if kw in text]
        sched_hits = [kw for kw in SCHEDULE if kw in text]
        all_hits = cb_hits + memo_hits + action_hits + sched_hits

        if not all_hits:
            return None

        slots = {}
        slots.update(sp.extract_phone(text))
        slots.update(sp.extract_name(text))
        slots.update(sp.extract_date(text))
        slots.update(sp.extract_time(text))

        # 메시지 내용 추출
        msg_m = MESSAGE_PATTERN.search(text)
        if msg_m:
            slots["message"] = (msg_m.group(1) or msg_m.group(2) or "").strip()

        # 서브 인텐트
        if cb_hits:
            slots["sub_intent"] = "callback"
        elif memo_hits:
            slots["sub_intent"] = "memo"
        elif sched_hits:
            slots["sub_intent"] = "schedule"
        else:
            slots["sub_intent"] = "action"

        # 콜백/메모는 명확한 액션이므로 가중치 높음
        kw_weight = len(cb_hits) * 2 + len(memo_hits) * 2 + len(sched_hits) * 2 + len(action_hits)
        confidence = sp.score(kw_weight, len(slots))

        return EngineResult(
            intent=self.intent,
            confidence=confidence,
            matched_keywords=all_hits,
            extracted_slots=slots,
        )
