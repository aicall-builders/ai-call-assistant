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
    updated_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_store_keyword (store_id, normalized_keyword),
    INDEX idx_store_enabled (store_id, is_enabled),
    INDEX idx_user_store (user_id, store_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
