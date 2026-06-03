"""
redis_client.py — Redis가 없거나 패키지가 없어도 Lambda가 죽지 않도록 fallback 처리.
"""
import os
import json
import logging

logger = logging.getLogger(__name__)

try:
    from redis.cluster import RedisCluster
    from redis.exceptions import RedisError, ConnectionError as RedisConnectionError
except Exception as import_error:
    RedisCluster = None
    RedisError = Exception
    RedisConnectionError = Exception
    logger.warning("[Redis] redis 패키지 없음. 캐시 없이 진행: %s", import_error)

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
REDIS_SSL = os.environ.get("REDIS_SSL", "true").lower() in {"1", "true", "yes", "y"}

TTL_KEYWORDS = 3600
TTL_FIREBASE_TOKEN = 3300
TTL_USER_INFO = 300
TTL_UPLOAD_LOCK = 600

_client = None


def get_redis():
    global _client
    if RedisCluster is None:
        return None
    try:
        if _client is None:
            _client = RedisCluster(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
                skip_full_coverage_check=True,
                ssl=REDIS_SSL,
                ssl_cert_reqs=None if REDIS_SSL else None,
            )
        _client.ping()
        return _client
    except Exception as e:
        logger.warning("[Redis] 연결 실패, 캐시 없이 진행: %s", e)
        _client = None
        return None


def cache_get(key):
    r = get_redis()
    if r is None:
        return None
    try:
        val = r.get(key)
        return json.loads(val) if val else None
    except Exception as e:
        logger.warning("[Redis] GET 실패 key=%s: %s", key, e)
        return None


def cache_set(key, value, ttl):
    r = get_redis()
    if r is None:
        return False
    try:
        r.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
        return True
    except Exception as e:
        logger.warning("[Redis] SET 실패 key=%s: %s", key, e)
        return False


def cache_delete(key):
    r = get_redis()
    if r is None:
        return False
    try:
        r.delete(key)
        return True
    except Exception as e:
        logger.warning("[Redis] DELETE 실패 key=%s: %s", key, e)
        return False


def set_nx_with_ttl(key, value, ttl):
    r = get_redis()
    if r is None:
        logger.warning("[Redis] 중복 체크 불가. Redis 연결 없음 → 통과 처리")
        return True
    try:
        return r.set(key, value, nx=True, ex=ttl) is True
    except Exception as e:
        logger.warning("[Redis] SET NX 실패 key=%s: %s", key, e)
        return True
