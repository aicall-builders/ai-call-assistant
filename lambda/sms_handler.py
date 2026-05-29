"""
sms_handler.py — 솔라피 SMS 발송
통화 분석 완료 후 고객에게 맞춤 문자 자동 발송
"""
import os
import json
import hmac
import hashlib
import logging
import uuid
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SOLAPI_API_KEY    = os.environ.get("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.environ.get("SOLAPI_API_SECRET", "")
SOLAPI_SENDER     = os.environ.get("SOLAPI_SENDER", "")
SOLAPI_API_URL    = "https://api.solapi.com/messages/v4/send"


# ── 인증 헤더 생성 ─────────────────────────────────────────────────────────────
def _get_auth_header() -> str:
    """솔라피 HMAC 인증 헤더 생성"""
    date      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt      = str(uuid.uuid4()).replace("-", "")
    signature = hmac.new(
        SOLAPI_API_SECRET.encode("utf-8"),
        f"{date}{salt}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"HMAC-SHA256 apiKey={SOLAPI_API_KEY}, date={date}, salt={salt}, signature={signature}"


# ── 전화번호 포맷 변환 ────────────────────────────────────────────────────────
def _format_phone(phone: str) -> str:
    phone = phone.strip().replace("-", "").replace(" ", "")
    if phone.startswith("+82"):
        phone = "0" + phone[3:]
    return phone


# ── SMS 발송 ──────────────────────────────────────────────────────────────────
def send_sms(to: str, message: str) -> bool:
    if not all([SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER]):
        logger.error("[SMS] 솔라피 환경변수 미설정")
        return False

    to_formatted = _format_phone(to)
    if not to_formatted:
        logger.error("[SMS] 수신번호 없음")
        return False

    if len(message) > 90:
        message = message[:90]

    payload = {
        "message": {
            "to":   to_formatted,
            "from": _format_phone(SOLAPI_SENDER),
            "text": message,
            "type": "SMS",
        }
    }

    try:
        resp = requests.post(
            SOLAPI_API_URL,
            headers={
                "Authorization": _get_auth_header(),
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info(f"[SMS] 발송 완료 to={to_formatted} result={result}")
        return True

    except requests.exceptions.HTTPError as e:
        logger.error(f"[SMS] HTTP 오류 to={to_formatted}: {e} body={resp.text}")
        return False
    except Exception as e:
        logger.error(f"[SMS] 발송 실패 to={to_formatted}: {e}")
        return False


# ── 통화 분석 결과 기반 자동 발송 ─────────────────────────────────────────────
def send_call_summary_sms(caller_number: str, nlp_result: dict) -> bool:
    sms = nlp_result.get("sms", {})
    if not sms.get("recommended"):
        logger.info("[SMS] 발송 불필요 (recommended=False)")
        return False

    message = sms.get("message", "").strip()
    if not message or not caller_number:
        logger.warning("[SMS] 수신번호 또는 문자 내용 없음")
        return False

    return send_sms(caller_number, message)


# ── Lambda 핸들러 (수동 발송용 API) ───────────────────────────────────────────
def lambda_handler(event: dict, context) -> dict:
    try:
        body    = json.loads(event.get("body") or "{}")
        to      = body.get("to", "").strip()
        message = body.get("message", "").strip()

        if not to or not message:
            return _response(400, {"error": "to, message 필수"})

        success = send_sms(to, message)
        if success:
            return _response(200, {"message": "발송 완료"})
        else:
            return _response(500, {"error": "발송 실패"})

    except Exception as e:
        logger.exception(f"[SMS] lambda_handler 오류: {e}")
        return _response(500, {"error": "내부 오류"})


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }