import os
import secrets
import logging
from pydantic_settings import BaseSettings
from functools import lru_cache

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    app_name: str = "电商数据分析API"
    app_version: str = "1.1.0"
    debug: bool = False

    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "ecommerce_analysis"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 3600

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 0
    redis_enabled: bool = False

    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    cors_origins: list[str] = ["http://localhost:8502", "http://localhost:8503", "http://localhost:8504", "http://localhost:8505", "http://localhost:8000"]

    default_page_size: int = 20
    max_page_size: int = 100

    cache_ttl_seconds: int = 300

    class Config:
        env_file = os.path.join(_BACKEND_DIR, ".env")
        env_file_encoding = "utf-8"

    def model_post_init(self, __context) -> None:
        if not self.jwt_secret:
            if self.debug:
                self.jwt_secret = "dev-only-insecure-secret-do-not-use-in-production"
                logger.warning("⚠️ JWT_SECRET 未设置，使用开发模式默认密钥，请勿用于生产环境！")
            else:
                raise ValueError("生产环境必须设置 JWT_SECRET 环境变量！")

    @property
    def database_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?charset=utf8mb4"
        )

    @property
    def async_database_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
