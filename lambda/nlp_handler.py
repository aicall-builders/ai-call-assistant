"""
nlp_handler.py — keywords.json 핫리로드 with Redis
변경점:
  - S3에서 keywords.json 읽을 때 Redis 캐시 우선 조회
  - TTL 1시간, 만료 시 자동 S3 재조회
  - POST /admin/reload-keywords 로 강제 무효화 가능
"""
import os
import json
import hashlib
import logging
import boto3
import openai

from botocore.exceptions import ClientError

from redis_client import cache_get, cache_set, cache_delete, TTL_KEYWORDS

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

BUCKET_NAME   = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")
KEYWORDS_KEY  = os.environ.get("KEYWORDS_S3_KEY", "config/keywords.json")
CACHE_KEY     = "nlp:keywords"          # Redis 키
CACHE_KEY_HASH = "nlp:keywords:hash"    # S3 ETag 저장 (변경 감지용)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
openai.api_key = OPENAI_API_KEY



# ── keywords 로딩 ──────────────────────────────────────────────────────────────

def load_keywords(force_reload: bool = False) -> dict:
    """
    keywords.json 로드 순서:
    1. force_reload=True  → 캐시 무효화 후 S3 강제 조회
    2. Redis 캐시 hit     → 즉시 반환
    3. Redis 캐시 miss    → S3 조회 후 캐싱
    """
    if force_reload:
        cache_delete(CACHE_KEY)
        cache_delete(CACHE_KEY_HASH)
        logger.info("[NLP] 강제 리로드: Redis 캐시 삭제")

    # 캐시 조회
    cached = cache_get(CACHE_KEY)
    if cached is not None:
        logger.info("[NLP] keywords 캐시 hit")
        return cached

    # S3 조회
    logger.info("[NLP] keywords 캐시 miss → S3 조회")
    try:
        response = s3.get_object(Bucket=BUCKET_NAME, Key=KEYWORDS_KEY)
        keywords = json.loads(response["Body"].read().decode("utf-8"))
        etag = response.get("ETag", "")

        cache_set(CACHE_KEY, keywords, TTL_KEYWORDS)
        cache_set(CACHE_KEY_HASH, etag, TTL_KEYWORDS)
        logger.info(f"[NLP] S3 keywords 로드 완료, ETag={etag}")
        return keywords

    except ClientError as e:
        logger.error(f"[NLP] S3 keywords 로드 실패: {e}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"[NLP] keywords.json 파싱 실패: {e}")
        return {}


def analyze_keywords(text: str, keywords: dict) -> dict:
    """텍스트에서 키워드 탐지 (기존 로직 유지, 구조만 예시)"""
    results = {}
    for category, word_list in keywords.items():
        found = [w for w in word_list if w in text]
        if found:
            results[category] = found
    return results

# ── 가드레일 상수 ──────────────────────────────────────────────────────────────
VALID_CATEGORIES = {"문의", "불만", "예약", "취소", "기타"}
VALID_SENTIMENTS = {"positive", "neutral", "negative"}


def _validate_gpt_result(result: dict) -> dict:
    """
    GPT 응답 유효성 검증 + 보정 (가드레일)
    - category / sentiment 허용값 범위 체크
    - action_required bool 타입 보정
    - keywords list 타입 보정
    - summary 빈값 보정
    """
    if result.get("category") not in VALID_CATEGORIES:
        logger.warning(f"[NLP] 비정상 category={result.get('category')} → '기타'로 보정")
        result["category"] = "기타"

    if result.get("sentiment") not in VALID_SENTIMENTS:
        logger.warning(f"[NLP] 비정상 sentiment={result.get('sentiment')} → 'neutral'로 보정")
        result["sentiment"] = "neutral"

    if not isinstance(result.get("action_required"), bool):
        result["action_required"] = False

    if not isinstance(result.get("keywords"), list):
        result["keywords"] = []

    if not result.get("summary", "").strip():
        result["summary"] = "요약 없음"

    if not isinstance(result.get("extracted_info"), dict):
        result["extracted_info"] = {}

    return result


def analyze_with_gpt(call_id: str, transcript: str) -> dict | None:
    prompt = f"""다음 통화 내용을 분석해주세요.

통화 내용:
{transcript}

아래 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{{
  "summary": "통화 내용 3줄 요약",
  "category": "문의/불만/예약/취소/기타 중 하나",
  "sentiment": "positive/neutral/negative 중 하나",
  "action_required": true 또는 false,
  "keywords": ["키워드1", "키워드2"],
  "extracted_info": {{
    "주요_요청": "...",
    "고객_의도": "..."
  }}
}}"""
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=55.0)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        raw = response.choices[0].message.content.strip()

        # JSON 마크다운 펜스 제거 (GPT가 ```json ... ``` 형태로 줄 때 대비)
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()

        result = json.loads(raw)

        # 가드레일 적용
        result = _validate_gpt_result(result)

        logger.info(f"[NLP] GPT 분석 완료 call_id={call_id} category={result['category']}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"[NLP] GPT 응답 파싱 실패 call_id={call_id}: {e} raw={raw[:100]}")
        return None
    except Exception as e:
        logger.error(f"[NLP] GPT 분석 오류 call_id={call_id}: {e}")
        return None



# ── Lambda 핸들러 ──────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    if event.get("call_id") and event.get("transcript"):
        result = analyze_with_gpt(event["call_id"], event["transcript"])
        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}

    path   = event.get("path", "")
    method = event.get("httpMethod", "POST")
    

    # ── 관리자용 강제 리로드 엔드포인트 ──────────────────────────────────────
    if path == "/admin/reload-keywords" and method == "POST":
        return _handle_force_reload(event)

    # ── 일반 NLP 분석 요청 ───────────────────────────────────────────────────
    return _handle_nlp(event)


def _handle_force_reload(event: dict) -> dict:
    """POST /admin/reload-keywords — keywords.json 강제 갱신"""
    # TODO: 실제 서비스에서는 Admin API Key 헤더 검증 추가
    headers = event.get("headers", {}) or {}
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
    """POST /nlp — 통화 텍스트 키워드 분석"""
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


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }