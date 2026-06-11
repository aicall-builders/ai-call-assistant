"""
calendar_handler.py — Google / Naver / Kakao 캘린더 연동 및 예약 카드 일정 생성

지원 엔드포인트
- GET    /calendar/connections
- GET    /calendar/connections/{provider}/authorize
- POST   /calendar/connections/oauth-code
- PATCH  /calendar/connections/default
- DELETE /calendar/connections/{provider}
- POST   /calls/{callId}/calendar-events
"""
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import boto3
import pymysql
import requests

from auth_handler import verify_firebase_token, get_db

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SUPPORTED_CALENDAR_PROVIDERS = {"google", "naver", "kakao"}
SEOUL_TIMEZONE = timezone(timedelta(hours=9))
CALENDAR_TIMEZONE = os.environ.get("CALENDAR_TIMEZONE", "Asia/Seoul")
CALENDAR_DEFAULT_DURATION_MINUTES = int(os.environ.get("CALENDAR_DEFAULT_DURATION_MINUTES", "60"))
CALENDAR_AUTO_MIGRATE_ENABLED = os.environ.get("CALENDAR_AUTO_MIGRATE", "true").lower() in {"1", "true", "yes", "y"}
CALENDAR_TOKEN_KMS_KEY_ID = os.environ.get("CALENDAR_TOKEN_KMS_KEY_ID") or os.environ.get("TOKEN_KMS_KEY_ID") or ""
kms_client = boto3.client("kms") if CALENDAR_TOKEN_KMS_KEY_ID else None


def _cors_origin(event=None):
    allowed_raw = os.environ.get("CORS_ALLOWED_ORIGINS") or os.environ.get("CORS_ALLOW_ORIGIN") or "*"
    allowed = [x.strip().rstrip("/") for x in allowed_raw.split(",") if x.strip()]
    if not allowed or "*" in allowed:
        return "*"
    headers = (event or {}).get("headers") or {}
    origin = (headers.get("origin") or headers.get("Origin") or "").rstrip("/")
    if origin in allowed:
        return origin
    if origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
        return origin
    return allowed[0]


def _response(status, body, event=None):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": _cors_origin(event),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
        },
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _normalize_path(event):
    path = event.get("rawPath") or event.get("path") or "/"
    stage = (event.get("requestContext") or {}).get("stage")
    if stage and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    elif stage and path == f"/{stage}":
        path = "/"
    return path or "/"


def _method(event):
    return (
        event.get("httpMethod")
        or (event.get("requestContext") or {}).get("http", {}).get("method")
        or "GET"
    ).upper()


def _query(event):
    return event.get("queryStringParameters") or {}


def _json_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _require_user(event):
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth_header.startswith("Bearer "):
        return None, _response(401, {"error": "인증 필요"}, event)
    decoded = verify_firebase_token(auth_header[7:])
    if not decoded:
        return None, _response(401, {"error": "유효하지 않은 토큰"}, event)
    firebase_uid = decoded.get("uid") or decoded.get("user_id") or decoded.get("sub")
    if not firebase_uid:
        return None, _response(401, {"error": "Firebase UID 없음"}, event)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE firebase_uid=%s LIMIT 1", (firebase_uid,))
                row = cur.fetchone()
        if not row:
            # DB에 없으면 firebase_uid를 user_id로 임시 사용
            logger.warning("[Calendar] 사용자 DB 미등록 firebase_uid=%s", firebase_uid)
            return firebase_uid, None
        return row["id"], None
    except Exception as e:
        logger.exception("[Calendar] 사용자 조회 실패: %s", e)
        return None, _response(500, {"error": "사용자 조회 실패"}, event)


def _ensure_schema():
    if not CALENDAR_AUTO_MIGRATE_ENABLED:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calendar_connections (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    provider VARCHAR(20) NOT NULL,
                    provider_account_id VARCHAR(191) NULL,
                    provider_email VARCHAR(255) NULL,
                    provider_nickname VARCHAR(255) NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NULL,
                    token_type VARCHAR(40) NULL,
                    expires_at DATETIME NULL,
                    scope TEXT NULL,
                    is_default TINYINT(1) NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_calendar_user_provider (user_id, provider),
                    INDEX idx_calendar_connections_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    call_id VARCHAR(64) NOT NULL,
                    provider VARCHAR(20) NOT NULL,
                    provider_event_id VARCHAR(255) NULL,
                    event_url TEXT NULL,
                    title VARCHAR(255) NULL,
                    start_at DATETIME NULL,
                    end_at DATETIME NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'created',
                    error_message TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_calendar_event_call_provider (call_id, provider),
                    INDEX idx_calendar_events_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()



def _table_columns(conn, table_name):
    """Return existing column names for a table. Used to tolerate older DB schemas during demo."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM `{table_name}`")
            rows = cur.fetchall() or []
        return {row.get("Field") for row in rows if row.get("Field")}
    except Exception as e:
        logger.warning("[Calendar] failed to inspect table columns table=%s error=%s", table_name, e)
        return set()


def _pick_existing(row, key, default=None):
    if not row:
        return default
    return row.get(key, default) if isinstance(row, dict) else default

def _encrypt_token(value):
    if not value:
        return None
    raw = value.encode("utf-8")
    if kms_client and CALENDAR_TOKEN_KMS_KEY_ID:
        blob = kms_client.encrypt(KeyId=CALENDAR_TOKEN_KMS_KEY_ID, Plaintext=raw)["CiphertextBlob"]
        return "kms:" + base64.b64encode(blob).decode("ascii")
    return "b64:" + base64.b64encode(raw).decode("ascii")


def _decrypt_token(value):
    if not value:
        return None
    if value.startswith("kms:"):
        if not kms_client:
            raise RuntimeError("KMS 토큰인데 kms_client가 없습니다")
        blob = base64.b64decode(value[4:])
        return kms_client.decrypt(CiphertextBlob=blob)["Plaintext"].decode("utf-8")
    if value.startswith("b64:"):
        return base64.b64decode(value[4:]).decode("utf-8")
    return value


def _env(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _calendar_provider_config(provider):
    if provider == "google":
        return {
            "client_id": _env("GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"),
            "client_secret": _env("GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"),
            "scope": _env("GOOGLE_CALENDAR_SCOPE", default="https://www.googleapis.com/auth/calendar.events"),
        }
    if provider == "naver":
        return {
            "client_id": _env("NAVER_CALENDAR_CLIENT_ID", "NAVER_OAUTH_CLIENT_ID", "NAVER_CLIENT_ID"),
            "client_secret": _env("NAVER_CALENDAR_CLIENT_SECRET", "NAVER_OAUTH_CLIENT_SECRET", "NAVER_CLIENT_SECRET"),
            "scope": _env("NAVER_CALENDAR_SCOPE", default="calendar"),
        }
    if provider == "kakao":
        return {
            "client_id": _env("KAKAO_CALENDAR_CLIENT_ID", "KAKAO_OAUTH_CLIENT_ID", "KAKAO_REST_API_KEY", "KAKAO_CLIENT_ID"),
            "client_secret": _env("KAKAO_CALENDAR_CLIENT_SECRET", "KAKAO_OAUTH_CLIENT_SECRET", "KAKAO_CLIENT_SECRET"),
            "scope": _env("KAKAO_CALENDAR_SCOPE", default="talk_calendar"),
        }
    raise ValueError("지원하지 않는 provider")


def _build_authorize_url(provider, redirect_uri, state):
    cfg = _calendar_provider_config(provider)
    if not cfg["client_id"]:
        raise RuntimeError(f"{provider} client_id 환경변수가 없습니다")
    if provider == "google":
        params = {
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": cfg["scope"],
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    if provider == "naver":
        params = {
            "response_type": "code",
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return "https://nid.naver.com/oauth2.0/authorize?" + urlencode(params)
    if provider == "kakao":
        params = {
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if cfg.get("scope"):
            params["scope"] = cfg["scope"]
        return "https://kauth.kakao.com/oauth/authorize?" + urlencode(params)


def _exchange_code(provider, code, redirect_uri, state=None):
    cfg = _calendar_provider_config(provider)
    if provider == "google":
        res = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": redirect_uri,
            "code": code,
        }, timeout=10)
    elif provider == "naver":
        res = requests.post("https://nid.naver.com/oauth2.0/token", data={
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "state": state or "",
        }, timeout=10)
    elif provider == "kakao":
        data = {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if cfg.get("client_secret"):
            data["client_secret"] = cfg["client_secret"]
        res = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=10)
    else:
        raise ValueError("지원하지 않는 provider")
    if res.status_code >= 400:
        raise RuntimeError(f"{provider} 토큰 교환 실패: HTTP {res.status_code} {res.text[:300]}")
    return res.json()


def _expires_at(expires_in):
    try:
        seconds = int(expires_in)
    except Exception:
        return None
    return datetime.utcnow() + timedelta(seconds=max(0, seconds - 60))


def _fetch_provider_profile(provider, access_token):
    try:
        if provider == "google":
            res = requests.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            if res.status_code < 400:
                data = res.json()
                return {"provider_account_id": str(data.get("sub") or ""), "provider_email": data.get("email") or "", "provider_nickname": data.get("name") or data.get("email") or "Google"}
        if provider == "naver":
            res = requests.get("https://openapi.naver.com/v1/nid/me", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            if res.status_code < 400:
                data = res.json().get("response") or {}
                return {"provider_account_id": str(data.get("id") or ""), "provider_email": data.get("email") or "", "provider_nickname": data.get("nickname") or data.get("name") or "Naver"}
        if provider == "kakao":
            res = requests.get("https://kapi.kakao.com/v2/user/me", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            if res.status_code < 400:
                data = res.json()
                account = data.get("kakao_account") or {}
                profile = account.get("profile") or {}
                return {"provider_account_id": str(data.get("id") or ""), "provider_email": account.get("email") or "", "provider_nickname": profile.get("nickname") or "Kakao"}
    except Exception as e:
        logger.info("[Calendar] profile fetch skipped provider=%s error=%s", provider, e)
    return {"provider_account_id": "", "provider_email": "", "provider_nickname": provider}


def _save_connection(user_id, provider, token_data):
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("access_token 발급 실패")
    refresh_token = token_data.get("refresh_token")
    expires_at = _expires_at(token_data.get("expires_in"))
    profile = _fetch_provider_profile(provider, access_token)

    # Some deployed DBs have an older calendar_connections schema.
    # Build INSERT/UPDATE dynamically from the columns that actually exist instead of failing on missing columns
    # such as provider_account_id.
    with get_db() as conn:
        cols = _table_columns(conn, "calendar_connections")
        if not cols:
            raise RuntimeError("calendar_connections 테이블을 찾을 수 없거나 컬럼 조회 실패")

        payload = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "provider": provider,
            "provider_account_id": profile.get("provider_account_id"),
            "provider_email": profile.get("provider_email"),
            "provider_nickname": profile.get("provider_nickname"),
            "access_token": _encrypt_token(access_token),
            "refresh_token": _encrypt_token(refresh_token),
            "token_type": token_data.get("token_type"),
            "expires_at": expires_at,
            "scope": token_data.get("scope"),
            "is_default": 0,
        }

        with conn.cursor() as cur:
            existing = None
            if {"user_id", "provider"}.issubset(cols):
                cur.execute("SELECT * FROM calendar_connections WHERE user_id=%s AND provider=%s LIMIT 1", (user_id, provider))
                existing = cur.fetchone()

            if "is_default" in cols:
                cur.execute("SELECT COUNT(*) AS cnt FROM calendar_connections WHERE user_id=%s", (user_id,))
                count = int((cur.fetchone() or {}).get("cnt") or 0)
                payload["is_default"] = 1 if count == 0 else 0

            if existing:
                update_payload = {k: v for k, v in payload.items() if k in cols and k not in {"id", "user_id", "provider"}}
                # Do not overwrite an existing refresh token with NULL when provider does not reissue it.
                if update_payload.get("refresh_token") is None:
                    update_payload.pop("refresh_token", None)
                if "updated_at" in cols:
                    update_payload["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
                if update_payload:
                    set_sql = ", ".join([f"`{k}`=%s" for k in update_payload.keys()])
                    cur.execute(
                        f"UPDATE calendar_connections SET {set_sql} WHERE user_id=%s AND provider=%s",
                        tuple(update_payload.values()) + (user_id, provider),
                    )
            else:
                insert_payload = {k: v for k, v in payload.items() if k in cols}
                if "created_at" in cols:
                    insert_payload["created_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
                if "updated_at" in cols:
                    insert_payload["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
                keys = list(insert_payload.keys())
                cur.execute(
                    f"INSERT INTO calendar_connections ({', '.join('`'+k+'`' for k in keys)}) VALUES ({', '.join(['%s']*len(keys))})",
                    tuple(insert_payload[k] for k in keys),
                )
        conn.commit()
    return _get_connection(user_id, provider)

def _get_connection(user_id, provider=None, default=False):
    with get_db() as conn:
        with conn.cursor() as cur:
            if provider:
                cur.execute("SELECT * FROM calendar_connections WHERE user_id=%s AND provider=%s LIMIT 1", (user_id, provider))
            elif default:
                cur.execute("SELECT * FROM calendar_connections WHERE user_id=%s ORDER BY is_default DESC, updated_at DESC LIMIT 1", (user_id,))
            else:
                return None
            return cur.fetchone()


def _public_connection(row):
    if not row:
        return None
    return {
        "id": row.get("id"),
        "provider": row.get("provider"),
        "provider_account_id": row.get("provider_account_id"),
        "provider_email": row.get("provider_email"),
        "provider_nickname": row.get("provider_nickname"),
        "expires_at": row.get("expires_at"),
        "scope": row.get("scope"),
        "is_default": bool(row.get("is_default")) if "is_default" in row else False,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }

def _list_connections(event, user_id):
    try:
        _ensure_schema()
    except Exception as e:
        logger.warning("[Calendar] schema ensure skipped: %s", e)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM calendar_connections WHERE user_id=%s", (user_id,))
                rows = cur.fetchall() or []
        def sort_key(row):
            return (0 if row.get("is_default") else 1, str(row.get("provider") or ""))
        return _response(200, {"connections": [_public_connection(row) for row in sorted(rows, key=sort_key)]}, event)
    except pymysql.err.ProgrammingError as e:
        # If the deployed schema is too old or missing, keep dashboard usable instead of returning 500.
        logger.warning("[Calendar] list connections fallback empty due schema error: %s", e)
        return _response(200, {"connections": [], "warning": "calendar_connections schema fallback"}, event)

"""
calendar_handler.py 패치 — 월 범위 조회 지원
==============================================

기존 _list_calendar_events 함수를 아래 버전으로 통째로 교체하세요.

변경점:
- date 단일 조회 + from/to 범위 조회 둘 다 지원
- from/to 있으면 그 범위, 없으면 date(기본 오늘) 하루
- 응답에 각 이벤트의 start_at(전체 날짜시간) 포함 → 앱에서 day 파싱
"""

def _list_calendar_events(event, user_id):
    """
    GET /calendar/events
        ?date=YYYY-MM-DD            (하루)
        ?from=YYYY-MM-DD&to=YYYY-MM-DD  (범위, 월 전체 조회용)
        &limit=100

    from/to 가 있으면 범위 조회, 없으면 date(기본 오늘) 하루 조회.
    """
    params = _query(event)

    date_from = params.get("from")
    date_to   = params.get("to")
    single    = params.get("date")

    # 범위 모드 vs 단일 모드 결정
    if date_from and date_to:
        try:
            d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
            d_to   = datetime.strptime(date_to,   "%Y-%m-%d").date()
        except ValueError:
            return _response(400, {"error": "from/to 형식은 YYYY-MM-DD"}, event)
        where_clause = "DATE(ce.start_at) BETWEEN %s AND %s"
        where_args = (d_from.strftime("%Y-%m-%d"), d_to.strftime("%Y-%m-%d"))
        resp_date = f"{date_from}~{date_to}"
    else:
        date_str = single or datetime.now(SEOUL_TIMEZONE).strftime("%Y-%m-%d")
        try:
            d_single = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return _response(400, {"error": "date 형식은 YYYY-MM-DD"}, event)
        where_clause = "DATE(ce.start_at) = %s"
        where_args = (d_single.strftime("%Y-%m-%d"),)
        resp_date = date_str

    try:
        limit = max(1, min(int(params.get("limit") or 100), 200))
    except (ValueError, TypeError):
        limit = 100

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        ce.id,
                        ce.provider,
                        ce.title,
                        ce.start_at,
                        ce.end_at,
                        ce.event_url,
                        ce.status,
                        c.caller_number,
                        s.category
                    FROM calendar_events ce
                    LEFT JOIN calls c ON c.id = ce.call_id
                    LEFT JOIN summaries s ON s.call_id = ce.call_id
                    WHERE ce.user_id = %s
                      AND ce.status = 'created'
                      AND {where_clause}
                    ORDER BY ce.start_at ASC
                    LIMIT %s
                """, (user_id, *where_args, limit))
                rows = cur.fetchall() or []

        events_out = []
        for row in rows:
            start_at = row.get("start_at")
            end_at   = row.get("end_at")
            time_str = start_at.strftime("%H:%M") if hasattr(start_at, "strftime") else str(start_at or "")[:5]
            end_str  = end_at.strftime("%H:%M")   if hasattr(end_at,   "strftime") else str(end_at   or "")[:5]

            desc_parts = []
            if row.get("category"):
                desc_parts.append(str(row["category"]))
            if row.get("caller_number"):
                desc_parts.append(str(row["caller_number"]))

            events_out.append({
                "id":          row.get("id"),
                "provider":    row.get("provider"),
                "title":       row.get("title") or "일정",
                "time":        time_str,
                "end_time":    end_str,
                "description": " · ".join(desc_parts),
                "event_url":   row.get("event_url"),
                "start_at":    str(start_at) if start_at else None,
                "end_at":      str(end_at)   if end_at   else None,
            })

        return _response(200, {
            "date":   resp_date,
            "events": events_out,
            "count":  len(events_out),
        }, event)

    except Exception as e:
        logger.exception("[Calendar] list events 실패 user_id=%s", user_id)
        return _response(500, {"error": str(e)}, event)

def _handle_authorize(event, user_id, provider):
    params = _query(event)
    redirect_uri = params.get("redirect_uri") or ""
    state = params.get("state") or ""
    if provider not in SUPPORTED_CALENDAR_PROVIDERS:
        return _response(400, {"error": "지원하지 않는 provider"}, event)
    if not redirect_uri or not state:
        return _response(400, {"error": "redirect_uri와 state가 필요합니다"}, event)
    try:
        return _response(200, {"authorize_url": _build_authorize_url(provider, redirect_uri, state)}, event)
    except Exception as e:
        logger.exception("[Calendar] authorize URL 생성 실패 provider=%s", provider)
        return _response(500, {"error": str(e)}, event)


def _handle_oauth_code(event, user_id):
    try:
        _ensure_schema()
    except Exception as e:
        logger.warning("[Calendar] schema ensure skipped: %s", e)
    body = _json_body(event)
    params = _query(event)
    provider = (body.get("provider") or params.get("provider") or "").lower()
    code = body.get("authorization_code") or body.get("code") or params.get("authorization_code") or params.get("code") or ""
    redirect_uri = body.get("redirect_uri") or params.get("redirect_uri") or ""
    state = body.get("state") or params.get("state") or ""
    if provider not in SUPPORTED_CALENDAR_PROVIDERS:
        return _response(400, {"error": "provider는 google/naver/kakao 중 하나여야 합니다"}, event)
    if not code or not redirect_uri:
        return _response(400, {"error": "code와 redirect_uri가 필요합니다"}, event)
    try:
        token_data = _exchange_code(provider, code, redirect_uri, state)
        conn = _save_connection(user_id, provider, token_data)
        return _response(200, {"connection": _public_connection(conn)}, event)
    except Exception as e:
        logger.exception("[Calendar] OAuth code 처리 실패 provider=%s", provider)
        return _response(500, {"error": str(e)}, event)


def _set_default(event, user_id):
    try:
        _ensure_schema()
    except Exception as e:
        logger.warning("[Calendar] schema ensure skipped: %s", e)
    provider = (_json_body(event).get("provider") or _query(event).get("provider") or "").lower()
    if provider not in SUPPORTED_CALENDAR_PROVIDERS:
        return _response(400, {"error": "provider는 google/naver/kakao 중 하나여야 합니다"}, event)
    with get_db() as conn:
        cols = _table_columns(conn, "calendar_connections")
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM calendar_connections WHERE user_id=%s AND provider=%s", (user_id, provider))
            if not cur.fetchone():
                return _response(404, {"error": "연결된 캘린더가 없습니다"}, event)
            if "is_default" in cols:
                cur.execute("UPDATE calendar_connections SET is_default=0 WHERE user_id=%s", (user_id,))
                cur.execute("UPDATE calendar_connections SET is_default=1 WHERE user_id=%s AND provider=%s", (user_id, provider))
        conn.commit()
    return _response(200, {"message": "기본 캘린더 설정 완료", "provider": provider}, event)

def _disconnect(event, user_id, provider):
    _ensure_schema()
    if provider not in SUPPORTED_CALENDAR_PROVIDERS:
        return _response(400, {"error": "지원하지 않는 provider"}, event)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM calendar_connections WHERE user_id=%s AND provider=%s", (user_id, provider))
        conn.commit()
    return _response(200, {"message": "캘린더 연결 해제 완료", "provider": provider}, event)


def _load_call_reservation(user_id, call_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.store_id, c.user_id, c.caller_number, c.created_at,
                       s.summary, s.category, s.extracted_info
                FROM calls c
                LEFT JOIN summaries s ON s.call_id = c.id
                WHERE c.id=%s AND c.user_id=%s
                ORDER BY s.created_at DESC
                LIMIT 1
            """, (call_id, user_id))
            row = cur.fetchone()
    if not row:
        return None
    info = row.get("extracted_info") or {}
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except Exception:
            info = {}
    if not isinstance(info, dict):
        info = {}
    row["extracted_info"] = info
    return row


def _normalize_date(value):
    if not value:
        return None
    text = str(value).strip()
    # YYYY-MM-DD 우선
    m = re.search(r"(20\d{2})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # MM-DD / M월 D일은 올해 기준
    m = re.search(r"(\d{1,2})[-./월\s]+(\d{1,2})", text)
    if m:
        now = datetime.now(SEOUL_TIMEZONE)
        mo, d = map(int, m.groups())
        return f"{now.year:04d}-{mo:02d}-{d:02d}"
    if text in {"오늘", "금일"}:
        return datetime.now(SEOUL_TIMEZONE).strftime("%Y-%m-%d")
    if text == "내일":
        return (datetime.now(SEOUL_TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d")
    return text if re.match(r"^\d{4}-\d{2}-\d{2}$", text) else None


def _normalize_time(value):
    if not value:
        return None
    text = str(value).strip().lower()
    ampm_pm = any(x in text for x in ["오후", "pm", "p.m"])
    ampm_am = any(x in text for x in ["오전", "am", "a.m"])
    m = re.search(r"(\d{1,2})(?:[:시\s]+(\d{1,2}))?", text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if ampm_pm and hour < 12:
        hour += 12
    if ampm_am and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _event_payload_from_call(call_row, request_body=None):
    """Build a provider calendar payload from call summary fields.

    Demo-safe behavior:
    - If the caller sends explicit date/time/title in the request body, use those first.
    - If AI summary has date/time, use those.
    - If neither exists, create a demo event tomorrow at 19:00 instead of failing.
      This keeps calendar integration testable even before STT/NLP is fully completed.
    """
    request_body = request_body or {}
    manual = request_body.get("event") if isinstance(request_body.get("event"), dict) else request_body
    info = call_row.get("extracted_info") or {}
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except Exception:
            info = {}
    if not isinstance(info, dict):
        info = {}

    date_value = (
        manual.get("date")
        or manual.get("reservation_date")
        or manual.get("start_date")
        or info.get("date")
        or info.get("reservation_date")
        or info.get("call_date")
    )
    time_value = (
        manual.get("time")
        or manual.get("reservation_time")
        or manual.get("start_time")
        or info.get("time")
        or info.get("reservation_time")
    )
    date_str = _normalize_date(date_value)
    time_str = _normalize_time(time_value)
    used_demo_fallback = False

    if date_str and time_str:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=SEOUL_TIMEZONE)
    else:
        now = datetime.now(SEOUL_TIMEZONE)
        start_dt = (now + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        used_demo_fallback = True

    try:
        duration_min = int(manual.get("duration_minutes") or CALENDAR_DEFAULT_DURATION_MINUTES)
    except Exception:
        duration_min = CALENDAR_DEFAULT_DURATION_MINUTES
    duration_min = max(10, min(duration_min, 24 * 60))
    end_dt = start_dt + timedelta(minutes=duration_min)

    customer = manual.get("customer_name") or info.get("customer_name") or info.get("name") or "고객"
    party_size = manual.get("party_size") or manual.get("people") or info.get("party_size") or info.get("people") or info.get("persons")

    title = manual.get("title")
    if not title:
        title_bits = ["예약"]
        if customer:
            title_bits.append(str(customer))
        if party_size:
            title_bits.append(f"{party_size}명")
        title = " - ".join(title_bits)
    if used_demo_fallback:
        title = "[테스트] " + str(title)

    menu = manual.get("menu") or info.get("menu") or info.get("items") or []
    if isinstance(menu, list):
        menu_text = ", ".join([str(x.get("name") if isinstance(x, dict) else x) for x in menu])
    else:
        menu_text = str(menu)

    desc_lines = [manual.get("description") or "AI 통화비서가 통화 카드에서 생성한 예약 일정입니다."]
    if used_demo_fallback:
        desc_lines.append("테스트용 기본 일정입니다. AI 요약에서 날짜/시간을 찾지 못해 내일 19:00으로 생성했습니다.")
    if call_row.get("summary"):
        desc_lines.append(f"요약: {call_row.get('summary')}")
    if call_row.get("caller_number"):
        desc_lines.append(f"전화번호: {call_row.get('caller_number')}")
    if menu_text:
        desc_lines.append(f"메뉴/항목: {menu_text}")
    if manual.get("special_notes") or info.get("special_notes"):
        desc_lines.append(f"특이사항: {manual.get('special_notes') or info.get('special_notes')}")

    payload = {
        "title": str(title)[:240],
        "description": "\n".join([line for line in desc_lines if line]),
        "start": start_dt,
        "end": end_dt,
        "location": manual.get("location") or manual.get("address") or info.get("location") or info.get("address") or "",
        "demo_fallback": used_demo_fallback,
    }
    return payload


def _refresh_connection_if_needed(connection):
    provider = connection["provider"]
    expires_at = connection.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
        except Exception:
            expires_at = None
    if not expires_at or expires_at > datetime.utcnow() + timedelta(minutes=3):
        return connection
    refresh_token = _decrypt_token(connection.get("refresh_token"))
    if not refresh_token:
        return connection
    cfg = _calendar_provider_config(provider)
    try:
        if provider == "google":
            res = requests.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": refresh_token,
            }, timeout=10)
        elif provider == "naver":
            res = requests.post("https://nid.naver.com/oauth2.0/token", data={
                "grant_type": "refresh_token",
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": refresh_token,
            }, timeout=10)
        elif provider == "kakao":
            data = {"grant_type": "refresh_token", "client_id": cfg["client_id"], "refresh_token": refresh_token}
            if cfg.get("client_secret"):
                data["client_secret"] = cfg["client_secret"]
            res = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=10)
        else:
            return connection
        if res.status_code >= 400:
            logger.warning("[Calendar] refresh 실패 provider=%s status=%s body=%s", provider, res.status_code, res.text[:200])
            return connection
        data = res.json()
        access_token = data.get("access_token")
        if not access_token:
            return connection
        expires_at_new = _expires_at(data.get("expires_in"))
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE calendar_connections SET access_token=%s, expires_at=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (_encrypt_token(access_token), expires_at_new, connection["id"]))
            conn.commit()
        connection["access_token"] = _encrypt_token(access_token)
        connection["expires_at"] = expires_at_new
    except Exception as e:
        logger.warning("[Calendar] refresh 예외 provider=%s error=%s", provider, e)
    return connection


def _create_google_event(access_token, payload):
    body = {
        "summary": payload["title"],
        "description": payload["description"],
        "location": payload.get("location") or "",
        "start": {"dateTime": payload["start"].isoformat(), "timeZone": CALENDAR_TIMEZONE},
        "end": {"dateTime": payload["end"].isoformat(), "timeZone": CALENDAR_TIMEZONE},
    }
    res = requests.post(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Google Calendar 일정 생성 실패: HTTP {res.status_code} {res.text[:300]}")
    data = res.json()
    return {"provider_event_id": data.get("id"), "event_url": data.get("htmlLink"), "raw": data}


def _escape_ics(value):
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _format_ics_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def _create_naver_event(access_token, payload):
    uid = str(uuid.uuid4())
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    ics = "\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:AI Call Assistant",
        "CALSCALE:GREGORIAN",
        "BEGIN:VTIMEZONE",
        f"TZID:{CALENDAR_TIMEZONE}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZNAME:GMT+09:00",
        "TZOFFSETFROM:+0900",
        "TZOFFSETTO:+0900",
        "END:STANDARD",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        "SEQUENCE:0",
        "CLASS:PUBLIC",
        "TRANSP:OPAQUE",
        f"UID:{uid}",
        f"DTSTART;TZID={CALENDAR_TIMEZONE}:{_format_ics_dt(payload['start'])}",
        f"DTEND;TZID={CALENDAR_TIMEZONE}:{_format_ics_dt(payload['end'])}",
        f"SUMMARY:{_escape_ics(payload['title'])}",
        f"DESCRIPTION:{_escape_ics(payload['description'])}",
        f"LOCATION:{_escape_ics(payload.get('location') or '')}",
        f"CREATED:{now_utc}",
        f"LAST-MODIFIED:{now_utc}",
        f"DTSTAMP:{now_utc}",
        "END:VEVENT",
        "END:VCALENDAR",
    ])
    res = requests.post(
        "https://openapi.naver.com/calendar/createSchedule.json",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"calendarId": "defaultCalendarId", "scheduleIcalString": ics},
        timeout=10,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Naver Calendar 일정 생성 실패: HTTP {res.status_code} {res.text[:300]}")
    data = res.json()
    event_id = ((data.get("returnValue") or {}).get("icalUid") or uid) if isinstance(data, dict) else uid
    return {"provider_event_id": event_id, "event_url": None, "raw": data}


def _create_kakao_event(access_token, payload):
    event = {
        "title": payload["title"],
        "time": {
            "start_at": payload["start"].isoformat(),
            "end_at": payload["end"].isoformat(),
            "time_zone": CALENDAR_TIMEZONE,
        },
        "description": payload["description"],
        "location": {"name": payload.get("location") or ""} if payload.get("location") else None,
    }
    event = {k: v for k, v in event.items() if v is not None}
    res = requests.post(
        "https://kapi.kakao.com/v2/api/calendar/create/event",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"event": json.dumps(event, ensure_ascii=False)},
        timeout=10,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Kakao Calendar 일정 생성 실패: HTTP {res.status_code} {res.text[:300]}")
    data = res.json()
    return {"provider_event_id": data.get("id") or data.get("event_id"), "event_url": None, "raw": data}


def _create_provider_event(provider, access_token, payload):
    if provider == "google":
        return _create_google_event(access_token, payload)
    if provider == "naver":
        return _create_naver_event(access_token, payload)
    if provider == "kakao":
        return _create_kakao_event(access_token, payload)
    raise ValueError("지원하지 않는 provider")


def _save_calendar_event_record(event_id, user_id, call_id, provider, provider_event_id=None, event_url=None, title=None, start_at=None, end_at=None, status="created", error_message=None):
    """Persist calendar event if the table/schema supports it; never block provider event creation."""
    try:
        with get_db() as conn:
            cols = _table_columns(conn, "calendar_events")
            if not cols:
                logger.warning("[Calendar] calendar_events table not found; provider event was created but local record was skipped")
                return
            payload = {
                "id": event_id,
                "user_id": user_id,
                "call_id": call_id,
                "provider": provider,
                "provider_event_id": provider_event_id,
                "event_url": event_url,
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "status": status,
                "error_message": error_message,
            }
            insert_cols = [c for c in payload.keys() if c in cols]
            if not {"id", "user_id", "call_id", "provider"}.issubset(set(insert_cols)):
                logger.warning("[Calendar] calendar_events schema too old; local record skipped cols=%s", sorted(cols))
                return
            values = [payload[c] for c in insert_cols]
            placeholders = ",".join(["%s"] * len(insert_cols))
            update_cols = [c for c in insert_cols if c != "id"]
            update_sql = ", ".join([f"`{c}`=VALUES(`{c}`)" for c in update_cols])
            if "updated_at" in cols:
                update_sql = (update_sql + ", " if update_sql else "") + "updated_at=CURRENT_TIMESTAMP"
            sql = f"""
                INSERT INTO calendar_events ({', '.join(f'`{c}`' for c in insert_cols)})
                VALUES ({placeholders})
                ON DUPLICATE KEY UPDATE {update_sql}
            """
            with conn.cursor() as cur:
                cur.execute(sql, values)
            conn.commit()
    except Exception as e:
        logger.warning("[Calendar] local calendar_events save skipped error=%s", e)


def _create_event_from_call(event, user_id, call_id):
    _ensure_schema()
    body = _json_body(event)
    provider = (body.get("provider") or "").lower() or None
    connection = _get_connection(user_id, provider=provider) if provider else _get_connection(user_id, default=True)
    if not connection:
        return _response(404, {"error": "연결된 캘린더가 없습니다"}, event)
    connection = _refresh_connection_if_needed(connection)
    provider = connection["provider"]
    access_token = _decrypt_token(connection.get("access_token"))
    if not access_token:
        return _response(500, {"error": "캘린더 access token이 없습니다"}, event)
    call = _load_call_reservation(user_id, call_id)
    if not call:
        return _response(404, {"error": "통화를 찾을 수 없습니다"}, event)
    try:
        payload = _event_payload_from_call(call, body)
    except ValueError as e:
        return _response(422, {"error": str(e)}, event)
    try:
        created = _create_provider_event(provider, access_token, payload)
        event_id = str(uuid.uuid4())
        _save_calendar_event_record(
            event_id=event_id,
            user_id=user_id,
            call_id=call_id,
            provider=provider,
            provider_event_id=created.get("provider_event_id"),
            event_url=created.get("event_url"),
            title=payload["title"],
            start_at=payload["start"].replace(tzinfo=None),
            end_at=payload["end"].replace(tzinfo=None),
            status="created",
            error_message=None,
        )
        return _response(201, {
            "success": True,
            "provider": provider,
            "provider_event_id": created.get("provider_event_id"),
            "event_url": created.get("event_url"),
            "title": payload["title"],
            "start_at": payload["start"].isoformat(),
            "end_at": payload["end"].isoformat(),
            "demo_fallback": payload.get("demo_fallback", False),
            "message": "캘린더 일정 생성 완료",
        }, event)
    except Exception as e:
        logger.exception("[Calendar] 일정 생성 실패 provider=%s call_id=%s", provider, call_id)
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO calendar_events (id, user_id, call_id, provider, status, error_message)
                        VALUES (%s,%s,%s,%s,'error',%s)
                        ON DUPLICATE KEY UPDATE status='error', error_message=VALUES(error_message), updated_at=CURRENT_TIMESTAMP
                    """, (str(uuid.uuid4()), user_id, call_id, provider, str(e)[:1000]))
                conn.commit()
        except Exception:
            pass
        return _response(500, {"error": str(e)}, event)


def lambda_handler(event, context):
    path = _normalize_path(event)
    method = _method(event)
    if method == "OPTIONS":
        return _response(200, {"message": "OK"}, event)

    user_id, err = _require_user(event)
    if err:
        return err

    parts = [p for p in path.split("/") if p]
    try:
        if path == "/calendar/events" and method == "GET":  
            return _list_calendar_events(event, user_id)
        if path == "/calendar/connections" and method == "GET":
            return _list_connections(event, user_id)
        if len(parts) == 4 and parts[:2] == ["calendar", "connections"] and parts[3] == "authorize" and method == "GET":
            return _handle_authorize(event, user_id, parts[2].lower())
        if path == "/calendar/connections/oauth-code" and method == "POST":
            return _handle_oauth_code(event, user_id)
        if path == "/calendar/connections/default" and method == "PATCH":
            return _set_default(event, user_id)
        if len(parts) == 3 and parts[:2] == ["calendar", "connections"] and method == "DELETE":
            return _disconnect(event, user_id, parts[2].lower())
        if len(parts) == 3 and parts[0] == "calls" and parts[2] == "calendar-events" and method == "POST":
            return _create_event_from_call(event, user_id, parts[1])
        return _response(404, {"error": "Not found", "path": path}, event)
    except Exception as e:
        logger.exception("[Calendar] 라우팅 처리 실패")
        return _response(500, {"error": str(e)}, event)
