-- ============================================================
-- AI 통화 비서 (call_recorder) — DB 스키마
-- MySQL 8.4.x / DEFAULT CHARSET=utf8mb4
-- ============================================================
-- 출처:
--   1) 각 Lambda 핸들러의 CREATE TABLE IF NOT EXISTS (auth/call/calendar/notes)
--   2) lambda/migrations/*.sql
--   3) calls / summaries / stores : 핸들러 SQL 쿼리에서 컬럼 복원 (★표시 = DB 직접 확인 권장)
--
-- 주의:
--   - 핸들러는 최초 실행 시 동일 DDL을 자동 생성하므로, 이 파일은 "참고/초기화용"입니다.
--   - calendar_connections / custom_keywords 는 핸들러본과 migration본 정의가 약간 다릅니다.
--     아래에는 migration본(최신, ENGINE/CHARSET 명시)을 우선 채택했습니다.
-- ============================================================

SET NAMES utf8mb4;

-- ========== 1. 사용자/인증 (auth_handler.py) ==========

CREATE TABLE IF NOT EXISTS users (
    id           VARCHAR(64) PRIMARY KEY,
    firebase_uid VARCHAR(128) UNIQUE,
    kakao_id     VARCHAR(64) UNIQUE NULL,
    name         VARCHAR(100) NULL,
    email        VARCHAR(255) NULL,
    role         VARCHAR(20) DEFAULT 'OWNER',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_social_accounts (
    id               VARCHAR(64) PRIMARY KEY,
    user_id          VARCHAR(64) NOT NULL,
    provider         VARCHAR(20) NOT NULL,
    provider_user_id VARCHAR(191) NOT NULL,
    email            VARCHAR(255) NULL,
    nickname         VARCHAR(255) NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_provider_user (provider, provider_user_id),
    UNIQUE KEY uniq_user_provider (user_id, provider),
    INDEX idx_social_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 2. 가게 (★ 복원 — DDL이 코드에 없어 최소 구조로 추정) ==========
-- store_id 가 calls/keywords 등 전반에서 FK로 사용됨. 실제 컬럼은 DB에서 SHOW CREATE TABLE stores; 로 확정 권장.

CREATE TABLE IF NOT EXISTS stores (
    id         VARCHAR(64) PRIMARY KEY,
    user_id    VARCHAR(64) NOT NULL,
    name       VARCHAR(255) NULL,
    domain     VARCHAR(50)  NULL,          -- 업종(7개 비즈니스 타입 스키마와 연동 추정)
    phone      VARCHAR(40)  NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_stores_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 3. 통화 (★ 복원 — call_handler.py 쿼리에서 컬럼 도출) ==========
-- INSERT/UPDATE/SELECT 쿼리에서 확인된 컬럼들. status 기본값 'uploaded', retry_count/updated_at 존재 확인.
-- 추가 컬럼이 더 있을 수 있으니 DB에서 SHOW CREATE TABLE calls; 로 최종 확정 권장.

CREATE TABLE IF NOT EXISTS calls (
    id            VARCHAR(64) PRIMARY KEY,
    store_id      VARCHAR(64) NOT NULL,
    user_id       VARCHAR(64) NOT NULL,
    caller_number VARCHAR(20)  NULL,
    s3_key        VARCHAR(512) NULL,
    status        VARCHAR(30)  NOT NULL DEFAULT 'uploaded',  -- uploaded → (STT/NLP) → 완료 등
    duration      INT          NULL DEFAULT 0,
    retry_count   INT          NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_calls_user (user_id),
    INDEX idx_calls_store (store_id),
    INDEX idx_calls_caller (caller_number),
    INDEX idx_calls_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 4. 요약 결과 (★ 복원 — calls LEFT JOIN summaries 쿼리에서 도출) ==========

CREATE TABLE IF NOT EXISTS summaries (
    call_id           VARCHAR(64) PRIMARY KEY,           -- calls.id 와 1:1
    summary           TEXT NULL,
    category          VARCHAR(50)  NULL,
    domain            VARCHAR(50)  NULL,
    sentiment         VARCHAR(20)  NULL,
    action_required   TINYINT(1)   NULL,
    keywords          JSON NULL,                          -- 또는 TEXT
    internal_keywords JSON NULL,
    extracted_info    JSON NULL,
    sms_recommended   TINYINT(1)   NULL,
    sms_message       TEXT NULL,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_summaries_call (call_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 5. 발신자 통계 (call_handler.py) ==========

CREATE TABLE IF NOT EXISTS caller_stats (
    id              VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id         VARCHAR(36) NOT NULL,
    store_id        VARCHAR(36) NOT NULL,
    caller_number   VARCHAR(20) NOT NULL,
    call_count      INT         NOT NULL DEFAULT 1,
    last_called_at  DATETIME    NOT NULL DEFAULT NOW(),
    first_called_at DATETIME    NOT NULL DEFAULT NOW(),
    updated_at      DATETIME    NOT NULL DEFAULT NOW() ON UPDATE NOW(),
    UNIQUE KEY uq_user_store_caller (user_id, store_id, caller_number),
    INDEX idx_user_id (user_id),
    INDEX idx_caller_number (caller_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 6. 커스텀 키워드 (migration본 채택, 최신) ==========

CREATE TABLE IF NOT EXISTS custom_keywords (
    id                 VARCHAR(36)  NOT NULL PRIMARY KEY,
    user_id            VARCHAR(36)  NOT NULL,
    store_id           VARCHAR(36)  NOT NULL,
    keyword            VARCHAR(100) NOT NULL,
    normalized_keyword VARCHAR(100) NOT NULL,
    label              VARCHAR(100) NULL,
    action_required    TINYINT(1)   NOT NULL DEFAULT 1,
    is_enabled         TINYINT(1)   NOT NULL DEFAULT 1,
    created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_store_keyword (store_id, normalized_keyword),
    INDEX idx_store_enabled (store_id, is_enabled),
    INDEX idx_user_store (user_id, store_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 7. 고객 프로필/분석 (call_handler.py) ==========

CREATE TABLE IF NOT EXISTS customer_profiles (
    id            VARCHAR(36)   NOT NULL PRIMARY KEY,
    user_id       VARCHAR(36)   NOT NULL,
    phone         VARCHAR(20)   NOT NULL,
    email         VARCHAR(200)  NULL,
    tendency      VARCHAR(500)  NULL,
    medical       VARCHAR(500)  NULL,
    special_notes VARCHAR(1000) NULL,
    custom_fields JSON          NULL,
    created_at    DATETIME      NOT NULL DEFAULT NOW(),
    updated_at    DATETIME      NOT NULL DEFAULT NOW() ON UPDATE NOW(),
    UNIQUE KEY uq_user_phone (user_id, phone),
    INDEX idx_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS customer_analysis (
    id           VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id      VARCHAR(36) NOT NULL,
    phone        VARCHAR(20) NOT NULL,
    analysis     TEXT        NULL,
    call_count   INT         NOT NULL DEFAULT 0,
    generated_at DATETIME    NOT NULL DEFAULT NOW(),
    UNIQUE KEY uq_user_phone_an (user_id, phone),
    INDEX idx_user_an (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ========== 8. 캘린더 연동 (migration본 채택, 최신) ==========

CREATE TABLE IF NOT EXISTS calendar_connections (
    id               VARCHAR(36) PRIMARY KEY,
    user_id          VARCHAR(36) NOT NULL,
    provider         VARCHAR(20) NOT NULL,
    provider_user_id VARCHAR(191) NULL,
    access_token     TEXT NOT NULL,                      -- KMS 암호화 토큰 저장
    refresh_token    TEXT NULL,
    expires_at       DATETIME NULL,
    scope            TEXT NULL,
    calendar_id      VARCHAR(255) NULL,
    calendar_name    VARCHAR(255) NULL,
    is_default       TINYINT(1) NOT NULL DEFAULT 0,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_calendar_connections_user_provider (user_id, provider),
    KEY idx_calendar_connections_user_default (user_id, is_default)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS calendar_event_logs (
    id                VARCHAR(36) PRIMARY KEY,
    user_id           VARCHAR(36) NOT NULL,
    call_id           VARCHAR(36) NOT NULL,
    provider          VARCHAR(20) NOT NULL,
    calendar_id       VARCHAR(255) NULL,
    external_event_id VARCHAR(255) NULL,
    event_url         TEXT NULL,
    title             VARCHAR(255) NOT NULL,
    start_at          DATETIME NOT NULL,
    end_at            DATETIME NOT NULL,
    status            VARCHAR(20) NOT NULL DEFAULT 'created',
    request_payload   LONGTEXT NULL,
    response_payload  LONGTEXT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_calendar_event_logs_call_provider (call_id, provider),
    KEY idx_calendar_event_logs_user_call (user_id, call_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- (참고) 핸들러본 calendar_events 테이블도 존재할 수 있음 (calendar_handler.py).
-- migration본 calendar_event_logs 와 역할이 겹치므로, 실제 운영 DB에서 어느 쪽을 쓰는지 확인 권장.

-- ========== 9. 통화 메모/사진 (notes_handler.py) ==========

CREATE TABLE IF NOT EXISTS call_notes (
    call_id    VARCHAR(64) NOT NULL PRIMARY KEY,
    user_id    VARCHAR(64) NOT NULL,
    memo       TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_call_notes_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS call_photos (
    id         VARCHAR(64) NOT NULL PRIMARY KEY,
    call_id    VARCHAR(64) NOT NULL,
    user_id    VARCHAR(64) NOT NULL,
    s3_key     VARCHAR(512) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_call_photos_call (call_id),
    INDEX idx_call_photos_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================
-- ★ 인계 메모:
--   - calls / summaries / stores 3개는 핸들러 SQL에서 컬럼을 복원한 것이라
--     실제 운영 DB와 100% 일치하지 않을 수 있습니다.
--   - 새 환경 구축 후, 가능하면 기존 DB에서 아래로 정본을 한 번 확보하세요:
--       mysqldump -h <HOST> -u admin -p --no-data call_recorder > schema_full.sql
--     (단, RDS가 private subnet이라 같은 VPC의 EC2/Lambda를 통해야 접속됩니다)
-- ============================================================
