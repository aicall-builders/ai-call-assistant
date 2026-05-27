-- 외부 캘린더 OAuth 연결/일정 등록 로그
-- Lambda calendar_handler.py는 CALENDAR_AUTO_MIGRATE=true이면 동일 DDL을 자동 실행한다.

CREATE TABLE IF NOT EXISTS calendar_connections (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(20) NOT NULL,
    provider_user_id VARCHAR(191) NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NULL,
    expires_at DATETIME NULL,
    scope TEXT NULL,
    calendar_id VARCHAR(255) NULL,
    calendar_name VARCHAR(255) NULL,
    is_default TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_calendar_connections_user_provider (user_id, provider),
    KEY idx_calendar_connections_user_default (user_id, is_default)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS calendar_event_logs (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    call_id VARCHAR(36) NOT NULL,
    provider VARCHAR(20) NOT NULL,
    calendar_id VARCHAR(255) NULL,
    external_event_id VARCHAR(255) NULL,
    event_url TEXT NULL,
    title VARCHAR(255) NOT NULL,
    start_at DATETIME NOT NULL,
    end_at DATETIME NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
    request_payload LONGTEXT NULL,
    response_payload LONGTEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_calendar_event_logs_call_provider (call_id, provider),
    KEY idx_calendar_event_logs_user_call (user_id, call_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
