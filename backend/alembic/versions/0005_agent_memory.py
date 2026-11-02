"""用户长期偏好：按主体隔离的结构化记忆表。

Revision ID: 0005_agent_memory
Revises: 0004_auth_identity
Create Date: 2026-11-02

设计要点：
- 只存白名单键值，不保存原始问答文本；
- (owner, memory_key) 唯一，同一偏好只保留最新值；
- owner 来自服务端认证主体，不能由客户端指定；
- 到期记录保留到惰性清理，召回时按 expires_at 过滤。
"""
from __future__ import annotations

from alembic import op

revision = "0005_agent_memory"
down_revision = "0004_auth_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_memory_fact (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            owner VARCHAR(100) NOT NULL,
            memory_key VARCHAR(64) NOT NULL,
            memory_value VARCHAR(255) NOT NULL,
            confidence DECIMAL(3,2) NOT NULL DEFAULT 0.95,
            source_thread_id VARCHAR(128) NOT NULL DEFAULT '',
            source_request_id VARCHAR(64) NOT NULL DEFAULT '',
            expires_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_agent_memory_owner_key (owner, memory_key),
            KEY idx_agent_memory_owner_expires (owner, expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_memory_fact")
