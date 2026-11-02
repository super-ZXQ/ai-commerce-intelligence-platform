"""历史情节：按主体隔离的单轮摘要与可选向量。

Revision ID: 0006_episode_memory
Revises: 0005_agent_memory
Create Date: 2026-11-02

设计要点：
- 只保存压缩后的单轮情节卡片，不保存完整对话与结果集；
- source_request_id 与 owner 唯一，重试不会重复写入；
- embedding 允许为空：向量模型不可用时由关键词召回降级；
- 到期记录由召回过滤，后续可增量清理。
"""
from __future__ import annotations

from alembic import op

revision = "0006_episode_memory"
down_revision = "0005_agent_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_memory_episode (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            owner VARCHAR(100) NOT NULL,
            thread_id VARCHAR(128) NOT NULL DEFAULT '',
            source_request_id VARCHAR(64) NOT NULL,
            summary TEXT NOT NULL,
            intent VARCHAR(32) NOT NULL DEFAULT '',
            embedding JSON NULL,
            embedding_model VARCHAR(64) NOT NULL DEFAULT '',
            expires_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_episode_owner_request (owner, source_request_id),
            KEY idx_episode_owner_created (owner, created_at),
            KEY idx_episode_owner_expires (owner, expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_memory_episode")
