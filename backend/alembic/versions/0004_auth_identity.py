"""认证与 AI 审计：数据库用户目录、服务凭据、登录/权限/AI 查询审计。

Revision ID: 0004_auth_identity
Revises: 0003_data_protection
Create Date: 2026-11-01

设计要点：
- 用户身份以数据库为唯一来源；token_version 用于立即吊销旧 JWT；
- 服务凭据只保存 SHA-256 哈希，密钥仅在创建时输出一次；
- 审计表只记录主体、动作、结果与用量元数据，不保存密码、JWT 或原始查询文本。
"""
from __future__ import annotations

from alembic import op

revision = "0004_auth_identity"
down_revision = "0003_data_protection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_users (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            username VARCHAR(100) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'viewer',
            scopes VARCHAR(512) NOT NULL DEFAULT '',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            token_version INT UNSIGNED NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            last_login_at DATETIME NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_auth_users_username (username)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS service_clients (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            name VARCHAR(100) NOT NULL,
            key_prefix VARCHAR(16) NOT NULL,
            key_hash CHAR(64) NOT NULL,
            scopes VARCHAR(512) NOT NULL DEFAULT '',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at DATETIME NULL,
            expires_at DATETIME NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_service_clients_name (name),
            UNIQUE KEY uq_service_clients_key_hash (key_hash),
            KEY idx_service_clients_prefix (key_prefix)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_audit_log (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            principal_type VARCHAR(20) NOT NULL,
            principal_name VARCHAR(100) NOT NULL,
            action VARCHAR(50) NOT NULL,
            resource VARCHAR(255) NOT NULL DEFAULT '',
            outcome VARCHAR(20) NOT NULL,
            client_ip VARCHAR(64) NOT NULL DEFAULT '',
            detail VARCHAR(500) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            KEY idx_auth_audit_created (created_at),
            KEY idx_auth_audit_principal (principal_name, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_query_audit (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            request_id VARCHAR(64) NOT NULL,
            principal_name VARCHAR(100) NOT NULL,
            thread_id VARCHAR(128) NOT NULL DEFAULT '',
            model_mode VARCHAR(20) NOT NULL DEFAULT 'managed',
            intent VARCHAR(32) NOT NULL DEFAULT '',
            outcome VARCHAR(20) NOT NULL,
            query_sha256 CHAR(64) NOT NULL,
            query_length INT UNSIGNED NOT NULL DEFAULT 0,
            input_tokens INT UNSIGNED NULL,
            output_tokens INT UNSIGNED NULL,
            total_tokens INT UNSIGNED NULL,
            latency_ms INT UNSIGNED NULL,
            error_category VARCHAR(80) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            KEY idx_ai_query_principal_created (principal_name, created_at),
            KEY idx_ai_query_created (created_at),
            KEY idx_ai_query_request (request_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_query_audit")
    op.execute("DROP TABLE IF EXISTS auth_audit_log")
    op.execute("DROP TABLE IF EXISTS service_clients")
    op.execute("DROP TABLE IF EXISTS auth_users")
