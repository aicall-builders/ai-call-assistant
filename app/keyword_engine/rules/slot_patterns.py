"""
모든 룰이 공통으로 사용하는 슬롯 추출용 정규식 패턴.
소상공인 한국어 전화 통화 기준.
"""
import re

# ── 날짜 ──────────────────────────────────────────────────────────────────────
# "4월 27일", "4/27", "04-27"
DATE_ABSOLUTE = re.compile(
    r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?"
    r"|(\d{1,2})[/\-](\d{1,2})"
)

# "오늘", "내일", "모레", "글피", "이번 주", "다음 주", "이번 달", "다음 달"
DATE_RELATIVE = re.compile(
    r"(글피|모레|내일|오늘|이번\s*주\s*말|이번\s*주|다음\s*주|이번\s*달\s*말|이번\s*달|다음\s*달)"
)

# "월요일", "화요일", ... / "이번 주 금요일"
DATE_WEEKDAY = re.compile(
    r"(이번\s*주|다음\s*주)?\s*(월|화|수|목|금|토|일)\s*요일"
)

# "3일 후", "2주 뒤", "한 달 있다가"
DATE_LATER = re.compile(
    r"(하루|이틀|사흘|(\d+)\s*(일|주|달))\s*(후|뒤|있다가|있다|지나서|지나면)"
)

# ── 시간 ──────────────────────────────────────────────────────────────────────
# "오전 10시", "오후 2시 30분", "저녁 6시 반"
TIME_CLOCK = re.compile(
    r"(오전|오후|새벽|아침|점심|저녁|밤)?\s*(\d{1,2})\s*시\s*(반|(\d{1,2})\s*분)?"
)

# "지금", "잠깐 후", "이따가", "조금 있다가"
TIME_RELATIVE = re.compile(
    r"(지금\s*당장|지금|잠깐\s*후|잠시\s*후|이따가|이따|나중에|조금\s*있다가|곧)"
)

# ── 금액 ──────────────────────────────────────────────────────────────────────
# "10만 원", "5천원", "3만 5천원", "1억"
AMOUNT = re.compile(
    r"(\d+)\s*억\s*(\d+)?\s*만?\s*원?"
    r"|(\d+)\s*만\s*(\d+)?\s*천?\s*원?"
    r"|(\d+)\s*천\s*원?"
    r"|(\d+)\s*원"
)

# ── 인원 ──────────────────────────────────────────────────────────────────────
PEOPLE = re.compile(r"(\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(명|분|인|사람)")

PEOPLE_KOR = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
               "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}

# ── 전화번호 ──────────────────────────────────────────────────────────────────
PHONE = re.compile(r"0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}")

# ── 이름 (성함) ───────────────────────────────────────────────────────────────
# "홍길동으로", "김 아무개 이름으로", "성함이" 뒤에 오는 2~4글자 한글
# non-greedy + lookahead: "홍길동으로" → "홍길동"만 캡처 (조사 포함 방지)
NAME = re.compile(
    r"(성함|이름|명의)\s*(?:은|는|이|가|으로|로)?\s*"
    r"([가-힣]{2,4}?)(?=\s|으로|로|은|는|이|가|을|를|이라|라고|$)"
)


def extract_date(text: str) -> dict:
    """텍스트에서 날짜 슬롯 추출. 매칭 없으면 빈 dict."""
    m = DATE_ABSOLUTE.search(text)
    if m:
        if m.group(1) and m.group(2):
            return {"date": f"{m.group(1)}월 {m.group(2)}일", "date_type": "absolute"}
        if m.group(3) and m.group(4):
            return {"date": f"{m.group(3)}/{m.group(4)}", "date_type": "absolute"}

    m = DATE_WEEKDAY.search(text)
    if m:
        prefix = (m.group(1) or "").replace(" ", "") + " " if m.group(1) else ""
        return {"date": f"{prefix}{m.group(2)}요일".strip(), "date_type": "weekday"}

    m = DATE_RELATIVE.search(text)
    if m:
        return {"date": m.group(1).strip(), "date_type": "relative"}

    m = DATE_LATER.search(text)
    if m:
        return {"date": m.group(0), "date_type": "relative"}

    return {}


def extract_time(text: str) -> dict:
    """텍스트에서 시간 슬롯 추출. 매칭 없으면 빈 dict."""
    m = TIME_CLOCK.search(text)
    if m:
        period = m.group(1) or ""
        hour = m.group(2)
        minute_part = m.group(3) or ""
        time_str = f"{period} {hour}시 {minute_part}".strip()
        return {"time": time_str, "time_type": "clock"}

    m = TIME_RELATIVE.search(text)
    if m:
        return {"time": m.group(1), "time_type": "relative"}

    return {}


def extract_amount(text: str) -> dict:
    """텍스트에서 금액 슬롯 추출. 가장 큰 금액 기준."""
    matches = AMOUNT.findall(text)
    if not matches:
        return {}
    # 원문에서 첫 번째 매칭 반환
    m = AMOUNT.search(text)
    return {"amount": m.group(0).replace(" ", "")} if m else {}


def extract_people(text: str) -> dict:
    """텍스트에서 인원 슬롯 추출."""
    m = PEOPLE.search(text)
    if not m:
        return {}
    raw = m.group(1)
    count = PEOPLE_KOR.get(raw, None)
    if count is None:
        try:
            count = int(raw)
        except ValueError:
            return {}
    return {"people": count}


def extract_phone(text: str) -> dict:
    m = PHONE.search(text)
    return {"phone": m.group(0).replace(" ", "")} if m else {}


def extract_name(text: str) -> dict:
    m = NAME.search(text)
    return {"name": m.group(2)} if m else {}


def score(keyword_hits: int, slot_count: int) -> float:
    """키워드 수 + 슬롯 수 기반 신뢰도 계산. 재현 가능하고 예측 가능한 스코어."""
    base = 0.5 + min(keyword_hits * 0.10, 0.30)   # 0.60 ~ 0.80
    bonus = min(slot_count * 0.08, 0.20)            # 0.00 ~ 0.20
    return round(min(base + bonus, 1.0), 4)
