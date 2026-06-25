"""
nlp_handler.py — 도메인 자동 파악 + 업종별 맞춤 키워드 + 문자 생성
변경점:
  - 도메인 자동 파악 (미용실/음식점/부동산/병원/네일샵/자동차정비/기타)
  - 업종별 맞춤 키워드 추출
  - 소상공인용 내부 정보 + 고객용 SMS 분리
  - 가드레일 강화
"""
import os
import json
import logging
import boto3
import pymysql
import pymysql.cursors
import openai

from botocore.exceptions import ClientError
from redis_client import cache_get, cache_set, cache_delete, TTL_KEYWORDS


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")
KEYWORDS_S3_KEY = os.environ.get("KEYWORDS_S3_KEY", "config/keywords.json")
KEYWORDS_CACHE_KEY = "nlp:keywords"
KEYWORDS_CACHE_HASH_KEY = "nlp:keywords:hash"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
openai.api_key = OPENAI_API_KEY

CUSTOM_KEYWORDS_CACHE_PREFIX = "nlp:custom_keywords:store"
TTL_CUSTOM_KEYWORDS = int(os.environ.get("TTL_CUSTOM_KEYWORDS", TTL_KEYWORDS))
VALID_EVENT_STATUSES = {"confirmed", "tentative", "cancelled", "rejected", "inquiry_only", "no_event"}
VALID_CALENDAR_ACTIONS = {"create", "update", "cancel", "none", "review_needed"}


def _mask_phone(phone) -> str:
    """전화번호 마스킹 (로그 PII 보호). 01012345678 -> 010****5678"""
    p = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(p) < 8:
        return "***"
    return p[:3] + "****" + p[-4:]


def _get_db_password() -> str:
    secret_id = os.environ.get("DB_SECRET_ARN") or os.environ.get("DB_SECRET_NAME") or ""
    if secret_id:
        try:
            sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))
            secret = sm.get_secret_value(SecretId=secret_id)
            data = json.loads(secret.get("SecretString") or "{}")
            return data.get("password") or data.get("db_password") or data.get("PASSWORD") or ""
        except Exception as e:
            logger.error(f"[DB] Secrets Manager 조회 실패: {e}")
    return os.environ.get("DB_PASSWORD", "")


def get_db():
    config = {
        "host": os.environ.get("DB_HOST", "call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com"),
        "user": os.environ.get("DB_USER", "admin"),
        "password": _get_db_password(),
        "db": os.environ.get("DB_NAME", "call_recorder"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 5,
    }
    return pymysql.connect(**config)


def _normalize_text(text: str) -> str:
    return "".join((text or "").lower().split())


def _custom_keywords_cache_key(store_id: str) -> str:
    return f"{CUSTOM_KEYWORDS_CACHE_PREFIX}:{store_id}"


def load_custom_keywords(store_id: str | None) -> list[dict]:
    if not store_id:
        return []

    cache_key = _custom_keywords_cache_key(store_id)
    cached = cache_get(cache_key)
    if isinstance(cached, list):
        logger.info(f"[NLP] custom_keywords 캐시 hit store_id={store_id}")
        return cached

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, keyword, normalized_keyword, label, action_required
                    FROM custom_keywords
                    WHERE store_id = %s AND is_enabled = 1
                    ORDER BY created_at DESC
                    """,
                    (store_id,),
                )
                rows = cur.fetchall()
        keywords = [
            {
                "id": row["id"],
                "keyword": row["keyword"],
                "normalized_keyword": row.get("normalized_keyword") or _normalize_text(row.get("keyword", "")),
                "label": row.get("label") or row.get("keyword"),
                "action_required": bool(row.get("action_required", True)),
            }
            for row in rows
        ]
        cache_set(cache_key, keywords, TTL_CUSTOM_KEYWORDS)
        logger.info(f"[NLP] custom_keywords DB 로드 store_id={store_id} count={len(keywords)}")
        return keywords
    except Exception as e:
        logger.error(f"[NLP] custom_keywords 로드 실패 store_id={store_id}: {e}")
        return []


def match_custom_keywords(text: str, custom_keywords: list[dict]) -> list[dict]:
    normalized_text = _normalize_text(text)
    matched = []
    for item in custom_keywords:
        normalized_keyword = item.get("normalized_keyword") or _normalize_text(item.get("keyword", ""))
        if normalized_keyword and normalized_keyword in normalized_text:
            matched.append({
                "id": item.get("id"),
                "keyword": item.get("keyword"),
                "label": item.get("label") or item.get("keyword"),
                "action_required": bool(item.get("action_required", True)),
                "source": "custom_keyword",
            })
    return matched


def match_base_keywords(text: str, keywords_config: dict, industry: str = "food") -> list[dict]:
    normalized_text = _normalize_text(text)
    matches = []

    for code, rule in (keywords_config.get("global_keywords") or {}).items():
        found = [word for word in rule.get("keywords", []) if _normalize_text(word) in normalized_text]
        if found:
            matches.append({
                "source": "base_global",
                "code": code,
                "label": rule.get("label", code),
                "priority": rule.get("priority", 99),
                "action_required": bool(rule.get("action_required", False)),
                "keywords": found,
            })

    categories = (
        keywords_config.get("industries", {})
        .get(industry, {})
        .get("categories", {})
    )
    for code, rule in categories.items():
        found = [word for word in rule.get("keywords", []) if _normalize_text(word) in normalized_text]
        if found:
            matches.append({
                "source": "base_industry",
                "code": code,
                "label": rule.get("label", code),
                "priority": rule.get("priority", 99),
                "action_required": bool(rule.get("action_required", False)),
                "keywords": found,
            })

    matches.sort(key=lambda item: item.get("priority", 99))
    return matches


def _flat_base_keywords(matches: list[dict]) -> list[str]:
    words = []
    for match in matches:
        words.extend(match.get("keywords", []))
    return list(dict.fromkeys(words))


def _build_keyword_context(base_matches: list[dict], custom_matches: list[dict]) -> str:
    lines = []
    if base_matches:
        lines.append("[기본 도메인 키워드 매칭]")
        for match in base_matches[:8]:
            lines.append(
                f"- {match.get('label') or match.get('code')}: "
                f"{', '.join(match.get('keywords', [])[:8])}"
            )
    if custom_matches:
        lines.append("[사용자 커스텀 중요 키워드 매칭]")
        for match in custom_matches[:20]:
            lines.append(f"- {match.get('label') or match.get('keyword')}: {match.get('keyword')}")
    return "\n".join(lines) if lines else "매칭된 키워드 없음"


def _merge_keyword_analysis(result: dict, base_matches: list[dict], custom_matches: list[dict]) -> dict:
    base_action = any(match.get("action_required") for match in base_matches)
    custom_action = any(match.get("action_required", True) for match in custom_matches)
    gpt_action = bool(result.get("action_required", False))

    result["action_required"] = bool(base_action or custom_action or gpt_action)

    importance_source = []
    if base_action:
        importance_source.append("base_rule")
    if custom_action:
        importance_source.append("custom_keyword")
    if gpt_action:
        importance_source.append("gpt")

    base_words = _flat_base_keywords(base_matches)
    custom_words = [match.get("keyword") for match in custom_matches if match.get("keyword")]
    existing_keywords = result.get("keywords", [])
    if not isinstance(existing_keywords, list):
        existing_keywords = []
    result["keywords"] = list(dict.fromkeys(base_words + custom_words + existing_keywords))

    internal = result.setdefault("internal", {})
    internal_keywords = internal.setdefault("keywords", {})
    internal_keywords["_matched_base_keywords"] = base_matches
    internal_keywords["_matched_custom_keywords"] = custom_matches
    internal_keywords["_importance_source"] = importance_source

    extracted = result.setdefault("extracted_info", {})
    extracted["matched_base_keywords"] = base_matches
    extracted["matched_custom_keywords"] = custom_matches
    extracted["importance_source"] = importance_source
    extracted.setdefault("event_status", result.get("event_status", "no_event"))
    extracted.setdefault("calendar_action", result.get("calendar_action", "none"))
    extracted.setdefault("calendar_candidate", result.get("calendar_candidate"))

    return result


# ── 도메인별 키워드 구조 정의 ──────────────────────────────────────────────────
DOMAIN_SCHEMA = {
    "미용실": {
        "시술":     "어떤 시술을 원하는지 (예: 커트+염색, 펌, 매직)",
        "일정":     "예약 날짜와 시간 (예: 6월 1일 오후 2시)",
        "고객상태": "단골/첫방문/재방문 여부",
        "두피상태": "두피/모발 상태 (예: 민감성 두피, 손상모)",
        "액션":     "소상공인이 준비해야 할 것 (예: 저자극 약품 준비)",
    },
    "음식점": {
        "예약인원": "몇 명인지 (예: 4인)",
        "일정":     "예약 날짜와 시간 (예: 6월 1일 저녁 7시)",
        "요청사항": "특별 요청 (예: 생일케이크, 룸 요청)",
        "식이제한": "식이 제한 사항 (예: 채식, 견과류 알러지)",
        "액션":     "준비해야 할 것 (예: 룸 세팅, 케이크 주문)",
    },
    "부동산": {
        "매물종류":  "매물 유형 (예: 아파트 전세, 빌라 월세)",
        "희망조건": "고객 조건 (예: 32평 3억 이하, 역세권)",
        "방문일정": "방문 희망 날짜 (예: 토요일 오후)",
        "고객성향": "고객 성향 (예: 급매 희망, 투자 목적)",
        "액션":     "준비해야 할 것 (예: 유사 매물 리스트 준비)",
    },
    "병원": {
        "진료과목": "진료 과목 (예: 정형외과, 내과)",
        "증상":     "주요 증상 (예: 허리 디스크 의심, 감기)",
        "일정":     "예약 날짜와 시간 (예: 6월 2일 오전 10시)",
        "진료유형": "초진/재진 여부",
        "액션":     "준비해야 할 것 (예: 차트 준비, MRI 예약)",
    },
    "네일샵": {
        "시술":     "시술 종류 (예: 젤네일 프렌치, 아트)",
        "일정":     "예약 날짜와 시간 (예: 6월 3일 오후 3시)",
        "고객상태": "단골/첫방문/재방문 여부",
        "네일상태": "현재 네일 상태 (예: 젤 제거 필요, 자연네일)",
        "액션":     "준비해야 할 것 (예: 제거 시간 포함, 재료 준비)",
    },
    "자동차정비": {
        "차량정보": "차종과 연식 (예: 소나타 2020 가솔린)",
        "정비항목": "정비 내용 (예: 엔진오일+타이어 교체)",
        "입고일정": "입고 날짜 (예: 내일 오전)",
        "차량상태": "차량 상태 (예: 정기 점검, 이상 소음)",
        "액션":     "준비해야 할 것 (예: 부품 재고 확인, 대차 준비)",
    },
    "기타": {
        "주요내용": "통화의 핵심 내용",
        "고객요청": "고객이 원하는 것",
        "일정":     "관련 날짜/시간",
        "고객상태": "고객 상황",
        "액션":     "처리해야 할 것",
    },
}

VALID_DOMAINS    = set(DOMAIN_SCHEMA.keys())
VALID_CATEGORIES = {"문의", "불만", "예약", "취소", "기타"}
VALID_SENTIMENTS = {"positive", "neutral", "negative"}


# ── keywords 로딩 ──────────────────────────────────────────────────────────────
def load_keywords(force_reload: bool = False) -> dict:
    if force_reload:
        cache_delete(KEYWORDS_CACHE_KEY)
        cache_delete(KEYWORDS_CACHE_HASH_KEY)
        logger.info("[NLP] 강제 리로드: Redis 캐시 삭제")

    # 캐시 조회
    cached = cache_get(KEYWORDS_CACHE_KEY)
    if cached is not None:
        logger.info("[NLP] keywords 캐시 hit")
        return cached

    logger.info("[NLP] keywords 캐시 miss → S3 조회")
    try:
        response = s3.get_object(Bucket=S3_BUCKET_NAME, Key=KEYWORDS_S3_KEY)
        keywords = json.loads(response["Body"].read().decode("utf-8"))
        etag = response.get("ETag", "")
        cache_set(KEYWORDS_CACHE_KEY, keywords, TTL_KEYWORDS)
        cache_set(KEYWORDS_CACHE_HASH_KEY, etag, TTL_KEYWORDS)
        logger.info(f"[NLP] S3 keywords 로드 완료, ETag={etag}")
        return keywords
    except ClientError as e:
        logger.error(f"[NLP] S3 keywords 로드 실패: {e}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"[NLP] keywords.json 파싱 실패: {e}")
        return {}


def analyze_keywords(text: str, keywords: dict) -> dict:
    results = {}
    for category, word_list in keywords.items():
        found = [w for w in word_list if w in text]
        if found:
            results[category] = found
    return results


# ── 가드레일 ──────────────────────────────────────────────────────────────────
def _validate_gpt_result(result: dict) -> dict:
    """GPT 응답 유효성 검증 + 보정"""

    # 도메인 검증
    if result.get("domain") not in VALID_DOMAINS:
        logger.warning(f"[NLP] 비정상 domain={result.get('domain')} → '기타'로 보정")
        result["domain"] = "기타"

    # category 검증
    if result.get("category") not in VALID_CATEGORIES:
        logger.warning(f"[NLP] 비정상 category={result.get('category')} → '기타'로 보정")
        result["category"] = "기타"

    # sentiment 검증
    if result.get("sentiment") not in VALID_SENTIMENTS:
        logger.warning(f"[NLP] 비정상 sentiment={result.get('sentiment')} → 'neutral'로 보정")
        result["sentiment"] = "neutral"

    # action_required 검증
    if not isinstance(result.get("action_required"), bool):
        result["action_required"] = False

    # internal 검증
    if not isinstance(result.get("internal"), dict):
        result["internal"] = {}

    internal = result["internal"]

    if not internal.get("summary", "").strip():
        internal["summary"] = "요약 없음"

    if not isinstance(internal.get("keywords"), dict):
        internal["keywords"] = {}

    # sms 검증
    if not isinstance(result.get("sms"), dict):
        result["sms"] = {"recommended": False, "message": ""}

    sms = result["sms"]
    if not isinstance(sms.get("recommended"), bool):
        sms["recommended"] = False

    if not isinstance(sms.get("message"), str):
        sms["message"] = ""

    # SMS 90자 초과 시 자르기
    if len(sms["message"]) > 90:
        sms["message"] = sms["message"][:90]

    # customer 검증  ← 여기부터 추가
    if not isinstance(result.get("customer"), dict):
        result["customer"] = {}
    cust = result["customer"]
    cust["name"]  = cust.get("name", "")  if isinstance(cust.get("name"), str)  else ""
    cust["phone"] = cust.get("phone", "") if isinstance(cust.get("phone"), str) else ""
    cust["phone"] = "".join(ch for ch in cust["phone"] if ch.isdigit())  # 숫자만
    # ← 여기까지

    # 캘린더/할 일 후보 검증
    if result.get("event_status") not in VALID_EVENT_STATUSES:
        result["event_status"] = "no_event"

    if result.get("calendar_action") not in VALID_CALENDAR_ACTIONS:
        result["calendar_action"] = "none"

    if result.get("calendar_candidate") is not None and not isinstance(result.get("calendar_candidate"), dict):
        result["calendar_candidate"] = None

    if not isinstance(result.get("extracted_info"), dict):
        result["extracted_info"] = {}

    result["extracted_info"].setdefault("event_status", result["event_status"])
    result["extracted_info"].setdefault("calendar_action", result["calendar_action"])
    result["extracted_info"].setdefault("calendar_candidate", result.get("calendar_candidate"))

    return result


# ── GPT 분석 ──────────────────────────────────────────────────────────────────
def analyze_with_gpt(call_id: str, transcript: str, base_matches: list[dict] | None = None, custom_matches: list[dict] | None = None) -> dict | None:

    base_matches = base_matches or []
    custom_matches = custom_matches or []

    # 도메인 스키마를 프롬프트에 포함
    domain_guide = "\n".join([
        f"- {domain}: {list(schema.keys())}"
        for domain, schema in DOMAIN_SCHEMA.items()
    ])
    keyword_context = _build_keyword_context(base_matches, custom_matches)

    prompt = f"""다음 통화 내용을 분석해주세요.

통화 내용:
{transcript}

[도메인 목록 및 키워드 구조]
{domain_guide}

[룰 기반 키워드 매칭 결과]
{keyword_context}

분석 규칙:
1. 통화 내용을 보고 도메인을 파악하세요.
2. 해당 도메인의 키워드 구조에 맞게 정보를 추출하세요.
3. 키워드는 실제 업종 종사자가 쓰는 용어로 구체적으로 작성하세요.
4. SMS는 고객이 읽었을 때 바로 이해할 수 있게 작성하세요. (90자 이내)
5. 예약/상담 완료된 경우에만 SMS recommended를 true로 설정하세요.
6. 사용자 커스텀 중요 키워드는 사용자가 중요하게 보는 업무 신호입니다. 해당 키워드가 날짜, 시간, 마감, 콜백, 방문, 납기, 발행 요청과 연결되면 calendar_candidate를 생성하세요.
7. 키워드가 포함되어도 문의만 했거나, 성사되지 않았거나, 보류/거절/취소 맥락이면 calendar_action을 create로 두지 마세요.

아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{{
  "domain": "미용실/음식점/부동산/병원/네일샵/자동차정비/기타 중 하나",
  "category": "문의/불만/예약/취소/기타 중 하나",
  "sentiment": "positive/neutral/negative 중 하나",
  "action_required": true 또는 false,
  "event_status": "confirmed/tentative/cancelled/rejected/inquiry_only/no_event 중 하나",
  "calendar_action": "create/update/cancel/none/review_needed 중 하나",
  "calendar_candidate": null 또는 {{
    "item_type": "reservation/callback/task/delivery/payment/deadline/other 중 하나",
    "title": "캘린더 또는 할 일 제목",
    "date": "YYYY-MM-DD 또는 빈 문자열",
    "time": "HH:mm 또는 빈 문자열",
    "description": "일정 설명",
    "source_keywords": ["캘린더 후보 판단에 사용된 키워드"]
  }},

  "customer": {{
    "name": "고객 성함 (언급 없으면 빈 문자열)",
    "phone": "고객 연락처, 숫자만 (예: 01012345678, 언급 없으면 빈 문자열)"
  }},

  "internal": {{
    "summary": "소상공인을 위한 통화 내용 3줄 요약",
    "keywords": {{
      "키워드항목1": "구체적인 값",
      "키워드항목2": "구체적인 값",
      "키워드항목3": "구체적인 값",
      "키워드항목4": "구체적인 값",
      "액션": "처리해야 할 것"
    }}
  }},

  "sms": {{
    "recommended": true 또는 false,
    "message": "고객이 이해하기 쉬운 문자 내용 (90자 이내)"
  }}
}}"""

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=55.0)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        raw = response.choices[0].message.content.strip()

        # JSON 마크다운 펜스 제거
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

        result = json.loads(raw)

        # 가드레일 적용
        result = _validate_gpt_result(result)

        logger.info(
            f"[NLP] GPT 분석 완료 call_id={call_id} "
            f"domain={result['domain']} category={result['category']} "
            f"sms_recommended={result['sms']['recommended']}"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"[NLP] GPT 응답 파싱 실패 call_id={call_id}: {e} raw={raw[:100]}")
        return None
    except Exception as e:
        logger.error(f"[NLP] GPT 분석 오류 call_id={call_id}: {e}")
        return None


# ── Lambda 핸들러 ──────────────────────────────────────────────────────────────
def lambda_handler(event: dict, context) -> dict:
    if event.get("task") == "customer_analysis":
        return _handle_customer_analysis(event)

    if event.get("call_id") and event.get("transcript"):
        transcript = event["transcript"]
        store_id = event.get("store_id") or ""
        keywords = load_keywords()
        base_matches = match_base_keywords(transcript, keywords) if keywords else []
        custom_matches = match_custom_keywords(transcript, load_custom_keywords(store_id))

        result = analyze_with_gpt(
            event["call_id"],
            transcript,
            base_matches=base_matches,
            custom_matches=custom_matches,
        )
        if result is None:
            result = {
                "domain": "기타",
                "category": "기타",
                "sentiment": "neutral",
                "action_required": False,
                "customer": {"name": "", "phone": ""},
                "internal": {"summary": "요약 없음", "keywords": {}},
                "sms": {"recommended": False, "message": ""},
                "extracted_info": {},
            }
        result = _merge_keyword_analysis(result, base_matches, custom_matches)
        # SMS는 소상공인이 직접 선택해서 발송 (자동 발송 제거)
        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}

    path   = event.get("path", "")
    method = event.get("httpMethod", "POST")

    if path == "/admin/reload-keywords" and method == "POST":
        return _handle_force_reload(event)

    return _handle_nlp(event)


def _handle_force_reload(event: dict) -> dict:
    headers   = event.get("headers", {}) or {}
    admin_key = headers.get("x-admin-key", "")
    if admin_key != os.environ.get("ADMIN_KEY", ""):
        return _response(403, {"error": "Forbidden"})

    keywords = load_keywords(force_reload=True)
    return _response(200, {
        "message": "keywords.json 리로드 완료",
        "category_count": len(keywords),
        "categories": list(keywords.keys()),
    })


def _handle_nlp(event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
        text = body.get("text", "").strip()
        if not text:
            return _response(400, {"error": "text 필드가 없습니다"})

        keywords = load_keywords()
        if not keywords:
            return _response(503, {"error": "keywords 로드 실패"})

        matched = analyze_keywords(text, keywords)
        return _response(200, {
            "matched": matched,
            "matched_count": sum(len(v) for v in matched.values()),
        })

    except Exception as e:
        logger.exception(f"[NLP] 처리 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _handle_customer_analysis(event: dict) -> dict:
    """
    고객의 통화 요약들을 종합해 한 단락짜리 'AI 고객 분석'을 생성.
    입력: {"task":"customer_analysis", "phone": "...", "summaries": ["[예약] ...", ...]}
    출력: {"analysis": "..."}
    """
    phone = event.get("phone") or ""
    summaries = event.get("summaries") or []
    if not summaries:
        return _response(200, {"analysis": ""})

    joined = "\n".join(f"- {s}" for s in summaries[:20])
    prompt = (
        "당신은 소상공인을 돕는 고객관리 비서입니다. "
        "아래는 한 고객과의 통화 요약 목록입니다. 이 고객의 특징을 사장님이 한눈에 파악할 수 있도록 "
        "2~3문장으로 자연스러운 한국어 단락으로 요약하세요. "
        "자주 묻는 내용, 선호/주의사항(예: 좌석 선호, 알레르기 등)이 있으면 반영하고, "
        "개인정보를 추측해 지어내지 마세요. 마크다운 없이 평문으로만 답하세요.\n\n"
        f"[통화 요약]\n{joined}\n\n[고객 분석]"
    )

    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=55.0)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=300,
        )
        text = response.choices[0].message.content.strip()
        logger.info(f"[NLP] 고객 분석 완료 phone={_mask_phone(phone)} len={len(text)}")
        return _response(200, {"analysis": text})
    except Exception as e:
        logger.error(f"[NLP] 고객 분석 오류 phone={_mask_phone(phone)}: {e}")
        return _response(500, {"analysis": "", "error": str(e)})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }
# redeploy nlp trigger