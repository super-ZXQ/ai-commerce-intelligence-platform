import logging
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel

from backend.config import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)

ALGORITHM = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.jwt_expire_minutes


class TokenData(BaseModel):
    username: str
    exp: int


class UserCredentials(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(username: str, expires_delta: timedelta | None = None) -> str:
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {"sub": username, "exp": expire, "iat": datetime.now(UTC)}
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> TokenData | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        exp: int = payload.get("exp")
        if username is None:
            return None
        return TokenData(username=username, exp=exp)
    except JWTError:
        return None


def _configured_users() -> dict[str, str]:
    users = {settings.admin_username: settings.admin_password}
    if settings.analyst_password:
        users[settings.analyst_username] = settings.analyst_password
    return users


def authenticate_user(username: str, password: str) -> bool:
    stored_password = _configured_users().get(username)
    if not stored_password:
        return False
    if stored_password.startswith(("$2a$", "$2b$", "$2y$")):
        return verify_password(password, stored_password)
    # 明文密码仅在 DEBUG 开发模式兼容；生产环境 .env 必须存 bcrypt 哈希，
    # 否则该账号直接拒绝登录（防止配置失误把明文凭据带上生产）。
    if settings.debug:
        logger.warning(
            "用户 %s 的密码以明文配置（仅开发模式兼容），建议改为 bcrypt 哈希", username,
        )
        return secrets.compare_digest(password, stored_password)
    logger.error("用户 %s 的密码未按 bcrypt 哈希配置，生产环境拒绝登录", username)
    return False


if __name__ == "__main__":
    # 生成 bcrypt 哈希的命令行工具，输出可写入 .env 的 ADMIN_PASSWORD / ANALYST_PASSWORD：
    #   python -m backend.utils.auth "你的明文密码"
    import sys

    if len(sys.argv) != 2:
        print("用法: python -m backend.utils.auth <明文密码>")
        raise SystemExit(1)
    print(_hash_password(sys.argv[1]))


def generate_api_key() -> str:
    return f"ea_{secrets.token_urlsafe(32)}"
