import logging
import os
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(_BACKEND_DIR, ".env"),
        env_file_encoding="utf-8",
    )

    app_name: str = "AI Commerce Intelligence Platform"
    app_version: str = "1.7.0"
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

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    redis_enabled: bool = False

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

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
    trust_proxy_headers: bool = False

    default_page_size: int = 20
    max_page_size: int = 100

    cache_ttl_seconds: int = 300

    def model_post_init(self, __context) -> None:  # noqa: PYI063
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
            if not self.admin_password.startswith(("$2a$", "$2b$", "$2y$")):
                raise ValueError("生产环境 ADMIN_PASSWORD 必须为 bcrypt 哈希（python -m backend.utils.auth <明文> 生成）！")
            if self.analyst_password and not self.analyst_password.startswith(("$2a$", "$2b$", "$2y$")):
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
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
