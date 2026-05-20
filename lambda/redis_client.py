"""
redis_client.py — Lambda 공통 Redis 연결 모듈
모든 핸들러에서 import해서 사용
"""
import os
import json
import logging
from redis.cluster import RedisCluster
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError

logger = logging.getLogger(__name__)

# ── 연결 설정 ──────────────────────────────────────────────────────────────────
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", None)
# ※ 클러스터 모드는 DB 선택(SELECT) 미지원 → REDIS_DB 없음

# TTL 상수 (초)
TTL_KEYWORDS       = 3600   # keywords.json 캐시: 1시간
TTL_FIREBASE_TOKEN = 3300   # Firebase 토큰 검증 결과: 55분
TTL_USER_INFO      = 300    # 유저 DB 조회: 5분
TTL_UPLOAD_LOCK    = 600    # 중복 업로드 락: 10분

_client = None  # Lambda 컨테이너 재사용을 위해 모듈 레벨 싱글턴


def get_redis() -> RedisCluster:
    """
    RedisCluster 클라이언트 싱글턴 반환.
    ElastiCache 클러스터 모드(clustercfg 엔드포인트) + TLS 대응.
    연결 실패 시 None 반환 → 호출부에서 캐시 miss 처리.
    """
    global _client
    try:
        if _client is None:
            _client = RedisCluster(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
                skip_full_coverage_check=True,  # ElastiCache 필수 옵션
                ssl=True,                        # ElastiCache TLS 필수 항목 대응
                ssl_cert_reqs=None,              # 인증서 검증 생략 (ElastiCache 자체 인증서)
            )
        _client.ping()
        return _client
    except (RedisConnectionError, RedisError) as e:
        logger.warning(f"[Redis] 연결 실패, 캐시 없이 진행: {e}")
        _client = None
        return None


# ── 공통 헬퍼 ──────────────────────────────────────────────────────────────────

def cache_get(key: str):
    """JSON 역직렬화 포함 GET. 실패 시 None."""
    r = get_redis()
    if r is None:
        return None
    try:
        val = r.get(key)
        return json.loads(val) if val else None
    except (RedisError, json.JSONDecodeError) as e:
        logger.warning(f"[Redis] GET 실패 key={key}: {e}")
        return None


def cache_set(key: str, value, ttl: int) -> bool:
    """JSON 직렬화 포함 SETEX. 성공 여부 반환."""
    r = get_redis()
    if r is None:
        return False
    try:
        r.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
        return True
    except (RedisError, TypeError) as e:
        logger.warning(f"[Redis] SET 실패 key={key}: {e}")
        return False


def cache_delete(key: str) -> bool:
    """키 삭제. 핫리로드 강제 무효화 시 사용."""
    r = get_redis()
    if r is None:
        return False
    try:
        r.delete(key)
        return True
    except RedisError as e:
        logger.warning(f"[Redis] DELETE 실패 key={key}: {e}")
        return False


def set_nx_with_ttl(key: str, value: str, ttl: int) -> bool:
    """
    원자적 SET NX (중복 방지 락).
    키가 없으면 set하고 True, 이미 있으면 False.
    """
    r = get_redis()
    if r is None:
        logger.warning("[Redis] 중복 체크 불가, Redis 연결 없음 → 통과 처리")
        return True
    try:
        result = r.set(key, value, nx=True, ex=ttl)
        return result is True
    except RedisError as e:
        logger.warning(f"[Redis] SET NX 실패 key={key}: {e}")
        return True  # 실패 시 통과 (안전 방향)
