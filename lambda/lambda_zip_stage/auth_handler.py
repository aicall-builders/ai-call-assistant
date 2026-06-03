"""
auth_handler.py — Kakao / Google / Naver 소셜 로그인 통합 핸들러

지원 엔드포인트
- GET  /auth/{provider}/authorize
- POST /auth/{provider}
- POST /auth/verify
- POST /auth/logout

Web은 authorization_code 방식, Android는 provider_access_token 방식을 사용한다.
Firebase Custom Token의 uid는 "{provider}:{provider_user_id}" 형식으로 통일한다.
"""
import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode

import boto3
import pymysql
import pymysql.cursors
import requests

import firebase_admin
from firebase_admin import auth as firebase_auth, credentials

try:
    from redis_client import cache_get, cache_set, cache_delete, TTL_FIREBASE_TOKEN, TTL_USER_INFO
except Exception:
    def cache_get(key): return None
    def cache_set(key, value, ttl): return False
    def cache_delete(key): return False
    TTL_FIREBASE_TOKEN = 3300
    TTL_USER_INFO = 300

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SUPPORTED_OAUTH_PROVIDERS = {"kakao", "google", "naver"}


def _init_firebase():
    if firebase_admin._apps:
        return
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    sa_b64 = os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64", "")
    if sa_b64:
        sa_json = base64.b64decode(sa_b64).decode("utf-8")
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_BASE64 또는 FIREBASE_SERVICE_ACCOUNT 환경변수가 필요합니다")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa_json)))


def _cors_origin(event=None):
    # Authorization 헤더 기반 호출만 사용하고 쿠키는 쓰지 않으므로 '*'도 안전하게 동작한다.
    allowed_raw = os.environ.get("CORS_ALLOWED_ORIGINS") or os.environ.get("CORS_ALLOW_ORIGIN") or "*"
    allowed = [x.strip().rstrip("/") for x in allowed_raw.split(",") if x.strip()]
    if not allowed or "*" in allowed:
        return "*"
    headers = (event or {}).get("headers") or {}
    origin = (headers.get("origin") or headers.get("Origin") or "").rstrip("/")
    if origin in allowed:
        return origin
    # 로컬 개발 편의
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


def _redirect(url, event=None):
    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            "Access-Control-Allow-Origin": _cors_origin(event),
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
        },
        "body": "",
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


def _json_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _query(event):
    return event.get("queryStringParameters") or {}


def _get_db_password():
    secret_id = os.environ.get("DB_SECRET_ARN") or os.environ.get("DB_SECRET_NAME") or ""
    if secret_id:
        try:
            sm = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))
            secret = sm.get_secret_value(SecretId=secret_id)
            data = json.loads(secret.get("SecretString") or "{}")
            return data.get("password") or data.get("db_password") or data.get("PASSWORD") or ""
        except Exception as e:
            logger.error("[DB] Secrets Manager 조회 실패: %s", e)
    return os.environ.get("DB_PASSWORD", "")


def get_db():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", ""),
        user=os.environ.get("DB_USER", "admin"),
        password=_get_db_password(),
        db=os.environ.get("DB_NAME", "call_recorder"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def _ensure_auth_schema():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id VARCHAR(64) PRIMARY KEY,
                    firebase_uid VARCHAR(128) UNIQUE,
                    kakao_id VARCHAR(64) UNIQUE NULL,
                    name VARCHAR(100) NULL,
                    email VARCHAR(255) NULL,
                    role VARCHAR(20) DEFAULT 'OWNER',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_social_accounts (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    provider VARCHAR(20) NOT NULL,
                    provider_user_id VARCHAR(191) NOT NULL,
                    email VARCHAR(255) NULL,
                    nickname VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_provider_user (provider, provider_user_id),
                    UNIQUE KEY uniq_user_provider (user_id, provider),
                    INDEX idx_social_user (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()


def _table_columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table,),
        )
        return {row["COLUMN_NAME"] for row in cur.fetchall()}


def _token_cache_key(token):
    return f"auth:token:{hashlib.sha256(token.encode()).hexdigest()}"


def verify_firebase_token(firebase_id_token):
    cache_key = _token_cache_key(firebase_id_token)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        _init_firebase()
        decoded = firebase_auth.verify_id_token(firebase_id_token, check_revoked=True)
        payload = {
            "uid": decoded.get("uid") or decoded.get("user_id") or decoded.get("sub"),
            "email": decoded.get("email", ""),
            "exp": decoded.get("exp", 0),
        }
        cache_set(cache_key, payload, TTL_FIREBASE_TOKEN)
        return payload
    except Exception as e:
        logger.warning("[Auth] Firebase token 검증 실패: %s", e)
        return None


def invalidate_token_cache(firebase_id_token):
    cache_delete(_token_cache_key(firebase_id_token))


def get_user_info(firebase_uid):
    cache_key = f"auth:user:{firebase_uid}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, firebase_uid, name, email, role, created_at FROM users WHERE firebase_uid=%s LIMIT 1",
                    (firebase_uid,),
                )
                user = cur.fetchone()
        if user:
            result = {k: str(v) if hasattr(v, "isoformat") else v for k, v in user.items()}
            cache_set(cache_key, result, TTL_USER_INFO)
            return result
    except Exception as e:
        logger.error("[Auth] get_user_info 실패: %s", e)
    return None


def invalidate_user_cache(firebase_uid):
    cache_delete(f"auth:user:{firebase_uid}")


def _env(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _oauth_provider_config(provider):
    provider = provider.lower()
    if provider == "google":
        return {
            "client_id": _env("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"),
            "client_secret": _env("GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"),
            "scope": _env("GOOGLE_OAUTH_SCOPE", "GOOGLE_LOGIN_SCOPE", default="openid email profile"),
        }
    if provider == "naver":
        return {
            "client_id": _env("NAVER_OAUTH_CLIENT_ID", "NAVER_CLIENT_ID"),
            "client_secret": _env("NAVER_OAUTH_CLIENT_SECRET", "NAVER_CLIENT_SECRET"),
            "scope": _env("NAVER_OAUTH_SCOPE", default=""),
        }
    if provider == "kakao":
        return {
            "client_id": _env("KAKAO_OAUTH_CLIENT_ID", "KAKAO_REST_API_KEY", "KAKAO_CLIENT_ID"),
            "client_secret": _env("KAKAO_OAUTH_CLIENT_SECRET", "KAKAO_CLIENT_SECRET"),
            "scope": _env("KAKAO_OAUTH_SCOPE", "KAKAO_LOGIN_SCOPE", default="profile_nickname"),
        }
    raise ValueError("지원하지 않는 provider")


def _build_authorize_url(provider, redirect_uri, state):
    cfg = _oauth_provider_config(provider)
    if not cfg["client_id"]:
        raise RuntimeError(f"{provider} client_id 환경변수가 없습니다")
    if provider == "google":
        params = {
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": cfg["scope"],
            "access_type": "online",
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
    cfg = _oauth_provider_config(provider)
    if not cfg["client_id"]:
        raise RuntimeError(f"{provider} client_id 환경변수가 없습니다")
    if provider == "google":
        data = {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": redirect_uri,
            "code": code,
        }
        res = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=10)
    elif provider == "naver":
        data = {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "state": state or "",
        }
        res = requests.post("https://nid.naver.com/oauth2.0/token", data=data, timeout=10)
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


def _fetch_profile(provider, access_token):
    if provider == "google":
        res = requests.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"Google 사용자 정보 조회 실패: HTTP {res.status_code} {res.text[:300]}")
        data = res.json()
        return {
            "provider": "google",
            "provider_user_id": str(data.get("sub") or ""),
            "email": data.get("email") or "",
            "nickname": data.get("name") or data.get("email") or "Google 사용자",
        }
    if provider == "naver":
        res = requests.get(
            "https://openapi.naver.com/v1/nid/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"Naver 사용자 정보 조회 실패: HTTP {res.status_code} {res.text[:300]}")
        data = res.json().get("response") or {}
        return {
            "provider": "naver",
            "provider_user_id": str(data.get("id") or ""),
            "email": data.get("email") or "",
            "nickname": data.get("nickname") or data.get("name") or "Naver 사용자",
        }
    if provider == "kakao":
        res = requests.get(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"Kakao 사용자 정보 조회 실패: HTTP {res.status_code} {res.text[:300]}")
        data = res.json()
        account = data.get("kakao_account") or {}
        profile = account.get("profile") or {}
        return {
            "provider": "kakao",
            "provider_user_id": str(data.get("id") or ""),
            "email": account.get("email") or "",
            "nickname": profile.get("nickname") or "Kakao 사용자",
        }
    raise ValueError("지원하지 않는 provider")


def _upsert_user(profile):
    _ensure_auth_schema()
    provider = profile["provider"]
    provider_user_id = str(profile["provider_user_id"])
    email = profile.get("email") or ""
    nickname = profile.get("nickname") or provider
    firebase_uid = f"{provider}:{provider_user_id}"
    if not provider_user_id:
        raise RuntimeError("provider_user_id가 비어 있습니다")

    with get_db() as conn:
        cols = _table_columns(conn, "users")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM user_social_accounts WHERE provider=%s AND provider_user_id=%s LIMIT 1",
                (provider, provider_user_id),
            )
            row = cur.fetchone()
            user_id = row["user_id"] if row else None

            if not user_id:
                cur.execute("SELECT id FROM users WHERE firebase_uid=%s LIMIT 1", (firebase_uid,))
                row = cur.fetchone()
                user_id = row["id"] if row else None

            if user_id:
                updates = []
                values = []
                if "firebase_uid" in cols:
                    updates.append("firebase_uid=%s"); values.append(firebase_uid)
                if "name" in cols:
                    updates.append("name=%s"); values.append(nickname)
                if "email" in cols:
                    updates.append("email=%s"); values.append(email)
                if provider == "kakao" and "kakao_id" in cols:
                    updates.append("kakao_id=%s"); values.append(provider_user_id)
                if updates:
                    values.append(user_id)
                    cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=%s", values)
            else:
                user_id = str(uuid.uuid4())
                insert_cols = ["id"]
                values = [user_id]
                if "firebase_uid" in cols:
                    insert_cols.append("firebase_uid"); values.append(firebase_uid)
                if "kakao_id" in cols and provider == "kakao":
                    insert_cols.append("kakao_id"); values.append(provider_user_id)
                if "name" in cols:
                    insert_cols.append("name"); values.append(nickname)
                if "email" in cols:
                    insert_cols.append("email"); values.append(email)
                if "role" in cols:
                    insert_cols.append("role"); values.append("OWNER")
                placeholders = ",".join(["%s"] * len(values))
                cur.execute(f"INSERT INTO users ({','.join(insert_cols)}) VALUES ({placeholders})", values)

            social_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO user_social_accounts (id, user_id, provider, provider_user_id, email, nickname)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    user_id=VALUES(user_id), email=VALUES(email), nickname=VALUES(nickname), updated_at=CURRENT_TIMESTAMP
                """,
                (social_id, user_id, provider, provider_user_id, email, nickname),
            )
        conn.commit()
    invalidate_user_cache(firebase_uid)
    return {
        "id": user_id,
        "firebase_uid": firebase_uid,
        "provider": provider,
        "provider_user_id": provider_user_id,
        "email": email,
        "nickname": nickname,
    }


def _handle_provider_authorize(event, provider):
    params = _query(event)
    redirect_uri = params.get("redirect_uri") or ""
    state = params.get("state") or ""
    if provider not in SUPPORTED_OAUTH_PROVIDERS:
        return _response(400, {"error": "지원하지 않는 provider"}, event)
    if not redirect_uri or not state:
        return _response(400, {"error": "redirect_uri와 state가 필요합니다"}, event)
    try:
        return _redirect(_build_authorize_url(provider, redirect_uri, state), event)
    except Exception as e:
        logger.exception("[Auth] authorize URL 생성 실패 provider=%s", provider)
        return _response(500, {"error": str(e)}, event)


def _handle_provider_login(event, provider):
    body = _json_body(event)
    provider = (body.get("provider") or provider or "").lower()
    if provider not in SUPPORTED_OAUTH_PROVIDERS:
        return _response(400, {"error": "provider는 kakao/google/naver 중 하나여야 합니다"}, event)

    try:
        access_token = body.get("provider_access_token") or body.get("access_token") or body.get(f"{provider}_access_token")
        if not access_token:
            code = body.get("authorization_code") or body.get("code")
            redirect_uri = body.get("redirect_uri") or ""
            state = body.get("state") or ""
            if not code or not redirect_uri:
                return _response(400, {"error": "provider_access_token 또는 authorization_code가 필요합니다"}, event)
            token_data = _exchange_code(provider, code, redirect_uri, state)
            access_token = token_data.get("access_token")
        if not access_token:
            return _response(400, {"error": "provider access token 발급 실패"}, event)

        profile = _fetch_profile(provider, access_token)
        user = _upsert_user(profile)
        _init_firebase()
        custom_token = firebase_auth.create_custom_token(user["firebase_uid"])
        custom_token_str = custom_token.decode("utf-8") if isinstance(custom_token, bytes) else custom_token
        return _response(200, {
            "firebase_custom_token": custom_token_str,
            "custom_token": custom_token_str,
            "firebase_uid": user["firebase_uid"],
            "uid": user["firebase_uid"],
            "user_id": user["id"],
            "user": user,
        }, event)
    except Exception as e:
        logger.exception("[Auth] provider login 실패 provider=%s", provider)
        return _response(500, {"error": str(e)}, event)


def _handle_verify(event):
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth_header.startswith("Bearer "):
        return _response(401, {"error": "No Authorization header"}, event)
    decoded = verify_firebase_token(auth_header[7:])
    if not decoded:
        return _response(401, {"error": "Invalid token"}, event)
    user = get_user_info(decoded.get("uid"))
    if not user:
        return _response(404, {"error": "User not found"}, event)
    return _response(200, {"user": user}, event)


def _handle_logout(event):
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        invalidate_token_cache(auth_header[7:])
    body = _json_body(event)
    uid = body.get("uid") or body.get("firebase_uid") or ""
    if uid:
        invalidate_user_cache(uid)
    return _response(200, {"message": "logged out"}, event)


def lambda_handler(event, context):
    path = _normalize_path(event)
    method = _method(event)
    if method == "OPTIONS":
        return _response(200, {"message": "OK"}, event)

    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "auth" and parts[2] == "authorize" and method == "GET":
        return _handle_provider_authorize(event, parts[1].lower())
    if len(parts) == 2 and parts[0] == "auth" and parts[1] in SUPPORTED_OAUTH_PROVIDERS and method == "POST":
        return _handle_provider_login(event, parts[1].lower())
    if path == "/auth/kakao" and method == "POST":
        return _handle_provider_login(event, "kakao")
    if path == "/auth/google" and method == "POST":
        return _handle_provider_login(event, "google")
    if path == "/auth/naver" and method == "POST":
        return _handle_provider_login(event, "naver")
    if path == "/auth/verify" and method == "POST":
        return _handle_verify(event)
    if path == "/auth/logout" and method == "POST":
        return _handle_logout(event)
    return _response(404, {"error": "Not found", "path": path}, event)
