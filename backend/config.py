from pydantic_settings import BaseSettings
from functools import lru_cache
import os

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


class Settings(BaseSettings):
    app_name: str = "电商数据分析API"
    app_version: str = "1.1.0"
    debug: bool = True

    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "ecommerce_analysis"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 3600

    llm_api_key: str = ""
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen-plus"

    jwt_secret: str = "change_this_secret_key_in_production"
    cors_origins: list[str] = ["*"]

    default_page_size: int = 20
    max_page_size: int = 100

    cache_ttl_seconds: int = 300

    class Config:
        env_file = os.path.join(_BACKEND_DIR, ".env")
        env_file_encoding = "utf-8"

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


@lru_cache()
def get_settings() -> Settings:
    return Settings()
