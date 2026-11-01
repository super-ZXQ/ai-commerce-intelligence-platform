import logging
import os
import re
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)

_INSECURE_JWT_SECRETS = {
    "change-this-secret-in-production",
    "your_jwt_secret_key_change_this_in_production",
}
_INSECURE_ADMIN_PASSWORDS = {
    "change-this-admin-password",
    "change-this-analyst-password",
}
# 完整的 bcrypt 结构校验：只检查前缀会把 Compose 误吞 $ 后的截断值当成合法哈希。
_BCRYPT_HASH_RE = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(_BACKEND_DIR, ".env"),
        env_file_encoding="utf-8",
    )

    app_name: str = "AI Commerce Intelligence Platform"
    app_version: str = "1.0.3"
    debug: bool = False

    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "ai_commerce_intelligence_platform"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 3600

    # AI Text-to-SQL 专用只读数据库账户（部署环境为 ea_ai，仅 SELECT 权限）。
    # 未配置时回落主库账户；配置后即使 SQL 黑名单被绕过也无法写入数据库。
    ai_db_user: str = ""
    ai_db_password: str = ""

    # 订单事件摄入专用写账号。Docker 中为 ea_events，只授予 orders/order_events
    # 的 SELECT/INSERT/UPDATE；绝不能传给 Agent 或 AI 助手。
    event_db_user: str = ""
    event_db_password: str = ""
    order_event_timeout_seconds: float = 5.0
    order_event_batch_max_size: int = 100

    # 认证目录专用账号。Docker 中为 ea_auth，仅能读写认证/审计表；
    # 未配置时回落主库账户（本地开发/测试）。
    auth_db_user: str = ""
    auth_db_password: str = ""
    # 统一登录会话 Cookie：HttpOnly，Nginx auth_request 用它保护 /ai/、/BI/ 与 WebSocket。
    auth_cookie_name: str = "ea_session"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"

    # AI 后端入口的容量与成本边界（按主体执行）。
    ai_max_concurrency: int = 2
    ai_queue_timeout_seconds: float = 10.0
    ai_query_timeout_seconds: float = 120.0
    ai_daily_request_limit: int = 200
    ai_max_inflight_per_user: int = 1

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    # 会话使用独立逻辑 DB，仅作为命名隔离；同一 Redis 实例的淘汰策略仍作用于全部 DB。
    redis_session_db: int = 1
    redis_enabled: bool = False

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    # managed：只用服务端平台密钥；byok：只用用户自带密钥。两者不能混用。
    llm_mode: str = "managed"
    allow_byok: bool = False
    # 逗号分隔的精确主机名列表；托管与 BYOK 共用同一出站白名单。
    llm_provider_allowlist: str = "api.deepseek.com"

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    admin_username: str = "admin"
    admin_password: str = ""
    analyst_username: str = "analyst"
    analyst_password: str = ""

    cors_origins: list[str] = Field(default_factory=lambda: [
        "http://localhost:8501",
        "http://localhost:8502",
        "http://localhost:8505",
        "http://localhost:8000",
    ])
    # 是否信任 X-Real-IP / X-Forwarded-For 作为限流客户端标识。
    # 反代（Nginx/Docker）部署必须开启：否则所有请求的 client.host 都是网关 IP，
    # 全体用户共享一个限流桶，单个高频用户即可让全站触发 429（变相 DoS）。
    # 直接对公网暴露时保持 False：XFF 第一个 IP 由客户端可控，开启可被伪造绕过限流。
    trust_proxy_headers: bool = False

    default_page_size: int = 20
    max_page_size: int = 100
    # 导出上限：CSV 流式返回，总量上限防止单请求长期占用连接；
    # Excel 需在内存构建工作簿，单独设置更低上限。
    export_max_rows: int = 200_000
    export_excel_max_rows: int = 50_000

    cache_ttl_seconds: int = 300

    @property
    def llm_allowed_hosts(self) -> tuple[str, ...]:
        return tuple(
            item.strip().lower().rstrip(".")
            for item in self.llm_provider_allowlist.split(",")
            if item.strip()
        )

    def model_post_init(self, __context) -> None:  # noqa: PYI063
        if self.llm_mode not in {"managed", "byok"}:
            raise ValueError("LLM_MODE 只能是 managed 或 byok")
        if self.allow_byok and not self.llm_allowed_hosts:
            raise ValueError("启用 BYOK 时必须配置 LLM_PROVIDER_ALLOWLIST")
        if not self.jwt_secret:
            if self.debug:
                self.jwt_secret = "dev-only-insecure-secret-do-not-use-in-production"
                logger.warning("⚠️ JWT_SECRET 未设置，使用开发模式默认密钥，请勿用于生产环境！")
            else:
                raise ValueError("生产环境必须设置 JWT_SECRET 环境变量！")
        if not self.admin_password:
            if self.debug:
                self.admin_password = "admin123"
                logger.warning("开发模式未设置 ADMIN_PASSWORD，使用本地默认密码")
            else:
                raise ValueError("生产环境必须设置 ADMIN_PASSWORD 环境变量！")
        if self.auth_db_user and not self.auth_db_password:
            raise ValueError("设置 AUTH_DB_USER 时必须同时设置 AUTH_DB_PASSWORD")
        if self.ai_max_concurrency < 1:
            raise ValueError("AI_MAX_CONCURRENCY 必须大于 0")
        if self.ai_max_inflight_per_user < 1:
            raise ValueError("AI_MAX_INFLIGHT_PER_USER 必须大于 0")
        if self.ai_daily_request_limit < 1:
            raise ValueError("AI_DAILY_REQUEST_LIMIT 必须大于 0")
        if self.ai_queue_timeout_seconds <= 0 or self.ai_query_timeout_seconds <= 0:
            raise ValueError("AI_QUEUE_TIMEOUT_SECONDS / AI_QUERY_TIMEOUT_SECONDS 必须大于 0")
        self.auth_cookie_samesite = self.auth_cookie_samesite.lower()
        if self.auth_cookie_samesite not in {"lax", "strict", "none"}:
            raise ValueError("AUTH_COOKIE_SAMESITE 只能是 lax、strict 或 none")
        if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
            raise ValueError("AUTH_COOKIE_SAMESITE=none 时必须同时开启 AUTH_COOKIE_SECURE")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.auth_cookie_name):
            raise ValueError("AUTH_COOKIE_NAME 只能包含字母、数字、下划线和连字符")
        if not 0 <= self.redis_db <= 15 or not 0 <= self.redis_session_db <= 15:
            raise ValueError("REDIS_DB / REDIS_SESSION_DB 必须在 0-15 之间")
        if self.redis_session_db == self.redis_db:
            # 逻辑 DB 只是命名隔离，不改变 allkeys-lru 的全局淘汰；但同库会直接键冲突。
            raise ValueError("REDIS_SESSION_DB 必须与 REDIS_DB 不同，避免会话键与业务缓存键冲突")
        if not self.debug:
            if self.jwt_secret in _INSECURE_JWT_SECRETS or len(self.jwt_secret) < 32:
                raise ValueError("生产环境 JWT_SECRET 至少需要 32 位且不能使用示例值！")
            if (
                self.admin_password in _INSECURE_ADMIN_PASSWORDS
                or len(self.admin_password) < 12
            ):
                raise ValueError("生产环境 ADMIN_PASSWORD 至少需要 12 位且不能使用示例值！")
            # 生产环境只接受 bcrypt 哈希（python -m backend.utils.auth 生成）：
            # 明文密码在 DEBUG=False 下登录会被拒，配置错误应启动即失败而非运行时才暴露。
            if not _BCRYPT_HASH_RE.fullmatch(self.admin_password):
                raise ValueError("生产环境 ADMIN_PASSWORD 必须为 bcrypt 哈希（python -m backend.utils.auth <明文> 生成）！")
            if self.analyst_password and not _BCRYPT_HASH_RE.fullmatch(self.analyst_password):
                raise ValueError("生产环境 ANALYST_PASSWORD 必须为 bcrypt 哈希（python -m backend.utils.auth <明文> 生成）！")

    @property
    def database_url(self) -> str:
        return (
            f"mysql+pymysql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?charset=utf8mb4"
        )

    @property
    def async_database_url(self) -> str:
        return (
            f"mysql+aiomysql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?charset=utf8mb4"
        )

    @property
    def ai_database_url(self) -> str:
        """AI Text-to-SQL 专用连接串：优先只读账户，未配置时回落主库账户。"""
        user = self.ai_db_user or self.db_user
        password = self.ai_db_password if self.ai_db_user else self.db_password
        return (
            f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def event_async_database_url(self) -> str:
        """事件写库连接串；未配置专用账号时仅为本地开发兼容而回落主账号。"""
        user = self.event_db_user or self.db_user
        password = self.event_db_password if self.event_db_user else self.db_password
        return (
            f"mysql+aiomysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def migration_database_url(self) -> str:
        """Alembic DDL 连接串：独立 migration 账号，禁止回落到 AI 只读账号。

        优先级：MIGRATION_DB_USER/PASSWORD 环境变量 → 主库管理账号（本地/CI）。
        Text-to-SQL 的 ea_ai 永不参与 migration。
        """
        user = os.environ.get("MIGRATION_DB_USER") or self.db_user
        password = os.environ.get("MIGRATION_DB_PASSWORD") or self.db_password
        return (
            f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def auth_async_database_url(self) -> str:
        """认证目录专用连接串：ea_auth 仅能访问认证/审计表，未配置时回落主账号。"""
        user = self.auth_db_user or self.db_user
        password = self.auth_db_password if self.auth_db_user else self.db_password
        return (
            f"mysql+aiomysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def redis_session_url(self) -> str:
        """会话逻辑 DB 连接串；只是命名隔离，淘汰策略仍由 Redis 实例全局决定。"""
        if self.redis_password:
            return (
                f"redis://:{self.redis_password}@{self.redis_host}:"
                f"{self.redis_port}/{self.redis_session_db}"
            )
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_session_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
