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

S3_BUCKET_NAME = os.environ.get("S3_BUCKET", "call-recoder-audio-1017")
KEYWORDS_S3_KEY = os.environ.get("KEYWORDS_S3_KEY", "config/keywords.json")
KEYWORDS_CACHE_KEY = "nlp:keywords"          # Redis 키
KEYWORDS_CACHE_HASH_KEY = "nlp:keywords:hash"    # S3 ETag 저장 (변경 감지용)

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
        cache_delete(KEYWORDS_CACHE_KEY)
        cache_delete(KEYWORDS_CACHE_HASH_KEY)
        logger.info("[NLP] 강제 리로드: Redis 캐시 삭제")

    # 캐시 조회
    cached = cache_get(KEYWORDS_CACHE_KEY)
    if cached is not None:
        logger.info("[NLP] keywords 캐시 hit")
        return cached

    # S3 조회
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
    """텍스트에서 키워드 탐지 (기존 로직 유지, 구조만 예시)"""
    results = {}
    for category, word_list in keywords.items():
        found = [w for w in word_list if w in text]
        if found:
            results[category] = found
    return results

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
        result = json.loads(raw)
        logger.info(f"[NLP] GPT 분석 완료 call_id={call_id}")
        return result
    except json.JSONDecodeError as e:
        logger.error(f"[NLP] GPT 응답 파싱 실패 call_id={call_id}: {e}")
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
