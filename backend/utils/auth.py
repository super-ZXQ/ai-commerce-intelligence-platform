"""数据库用户目录、JWT 会话、服务凭据与权限依赖。

身份的唯一来源是数据库（auth_users / service_clients）。环境变量里的
ADMIN_PASSWORD / ANALYST_PASSWORD 只用于空目录时的首次引导，之后修改密码、
停用账号、吊销 JWT 都必须走 manage_auth.py 或数据库，避免出现“环境变量身份”
和“数据库身份”两套来源。

审计与令牌约定：
- JWT 只携带 sub / exp / iat / tv / role / scopes；业务鉴权每次都重新读库，
  token_version 不匹配或账号停用会立即失效；
- 服务凭据只保存 SHA-256 哈希，明文只在创建时输出一次；
- 日志与审计不记录密码、JWT、API Key 或原始问题文本。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWTError
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_auth_db
from backend.models.database_models import AuthUser, ServiceClient
from backend.utils.text_cleaner import sanitize_error

settings = get_settings()

logger = logging.getLogger(__name__)

ALGORITHM = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.jwt_expire_minutes

# 权限点是业务路由的唯一判定依据；角色只是默认权限集合。
SCOPE_DATA_READ = "data:read"
SCOPE_DATA_EXPORT = "data:export"
SCOPE_AI_QUERY = "ai:query"
SCOPE_EVENTS_WRITE = "events:write"
SCOPE_ADMIN_MANAGE = "admin:manage"

KNOWN_SCOPES = frozenset(
    {SCOPE_DATA_READ, SCOPE_DATA_EXPORT, SCOPE_AI_QUERY, SCOPE_EVENTS_WRITE, SCOPE_ADMIN_MANAGE}
)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": KNOWN_SCOPES,
    "analyst": frozenset({SCOPE_DATA_READ, SCOPE_AI_QUERY}),
    "viewer": frozenset({SCOPE_DATA_READ, SCOPE_AI_QUERY}),
    "ingest": frozenset({SCOPE_EVENTS_WRITE}),
}


class TokenData(BaseModel):
    username: str
    exp: int
    role: str = "viewer"
    scopes: tuple[str, ...] = ()
    token_version: int = 1


class UserCredentials(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class Principal(BaseModel):
    """一次请求解析出的调用主体；权限以数据库当前值为准。"""

    principal_type: str
    name: str
    role: str = ""
    scopes: frozenset[str] = frozenset()
    token_version: int = 0

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def as_dict(self) -> dict:
        return {
            "username": self.name,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "principal_type": self.principal_type,
        }


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def is_bcrypt_hash(value: str) -> bool:
    return value.startswith(("$2a$", "$2b$", "$2y$"))


def normalize_scopes(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def effective_scopes(role: str, explicit: str | None) -> frozenset[str]:
    """角色默认权限与显式权限取并集；未知角色只拥有显式权限。"""
    return ROLE_SCOPES.get(role, frozenset()) | normalize_scopes(explicit)


def role_default_scopes(role: str) -> str:
    return ",".join(sorted(ROLE_SCOPES.get(role, frozenset())))


def hash_service_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def create_access_token(
    username: str,
    expires_delta: timedelta | None = None,
    *,
    role: str = "viewer",
    scopes: list[str] | tuple[str, ...] | None = None,
    token_version: int = 1,
) -> str:
    now = datetime.now(UTC)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = {
        "sub": username,
        "exp": expire,
        "iat": now,
        "tv": token_version,
        "role": role,
        "scopes": list(scopes or ()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> TokenData | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp", "iat"]},
        )
        username: str = payload.get("sub")
        exp: int = payload.get("exp")
        if username is None:
            return None
        scopes = payload.get("scopes") or ()
        if not isinstance(scopes, (list, tuple)):
            return None
        return TokenData(
            username=username,
            exp=exp,
            role=str(payload.get("role") or "viewer"),
            scopes=tuple(str(item) for item in scopes),
            token_version=int(payload.get("tv") or 0),
        )
    except (PyJWTError, TypeError, ValueError):
        return None


def _bootstrap_password_hash(stored: str) -> str:
    if is_bcrypt_hash(stored):
        return stored
    if settings.debug:
        logger.warning("空用户目录引导：开发模式接受明文引导密码并即时哈希")
        return hash_password(stored)
    raise ValueError("生产环境引导密码必须为 bcrypt 哈希")


async def ensure_bootstrap_users(session: AsyncSession) -> int:
    """仅在 auth_users 为空时用环境变量创建首管理员与分析员。

    已有任何用户时绝不覆盖或重置密码：避免部署时误把生产账号密码
    回滚成 .env 中的旧值。
    """
    count = await session.scalar(select(func.count()).select_from(AuthUser))
    if count:
        return 0
    users = [
        AuthUser(
            username=settings.admin_username,
            password_hash=_bootstrap_password_hash(settings.admin_password),
            role="admin",
            scopes="",
            is_active=1,
            token_version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    ]
    if settings.analyst_password:
        users.append(
            AuthUser(
                username=settings.analyst_username,
                password_hash=_bootstrap_password_hash(settings.analyst_password),
                role="analyst",
                scopes="",
                is_active=1,
                token_version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    session.add_all(users)
    try:
        await session.commit()
        logger.info("认证目录为空，已按环境变量引导 %d 个账号", len(users))
        return len(users)
    except IntegrityError:
        # 多 worker 同时启动时由唯一键兜底，不覆盖已存在账号。
        await session.rollback()
        return 0


async def authenticate_user(
    session: AsyncSession, username: str, password: str
) -> AuthUser | None:
    """校验数据库用户；停用账号与错误密码返回同一结果，避免账号枚举。"""
    await ensure_bootstrap_users(session)
    user = (
        await session.execute(select(AuthUser).where(AuthUser.username == username))
    ).scalar_one_or_none()
    if user is None or not bool(user.is_active):
        # 仍执行一次 bcrypt 校验，避免“用户不存在”响应明显更快。
        verify_password(password, "$2b$12$" + "x" * 53)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def load_principal_from_token(session: AsyncSession, token: str) -> Principal | None:
    token_data = decode_token(token)
    if token_data is None:
        return None
    user = (
        await session.execute(select(AuthUser).where(AuthUser.username == token_data.username))
    ).scalar_one_or_none()
    if user is None or not bool(user.is_active):
        return None
    if int(user.token_version) != token_data.token_version:
        return None
    return Principal(
        principal_type="user",
        name=user.username,
        role=user.role,
        scopes=effective_scopes(user.role, user.scopes),
        token_version=int(user.token_version),
    )


async def load_service_principal(session: AsyncSession, api_key: str) -> Principal | None:
    key_hash = hash_service_key(api_key)
    client = (
        await session.execute(select(ServiceClient).where(ServiceClient.key_hash == key_hash))
    ).scalar_one_or_none()
    if client is None or not bool(client.is_active):
        return None
    if client.expires_at is not None and client.expires_at <= datetime.now(UTC).replace(tzinfo=None):
        return None
    scopes = normalize_scopes(client.scopes) & KNOWN_SCOPES
    return Principal(
        principal_type="service",
        name=client.name,
        role="service",
        scopes=scopes,
    )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip()
    cookie = request.cookies.get(settings.auth_cookie_name)
    return cookie.strip() if cookie else None


def client_ip(request: Request) -> str:
    from backend.utils.rate_limiter import _get_client_id

    return _get_client_id(request, trust_proxy_headers=settings.trust_proxy_headers)


async def get_principal(
    request: Request,
    auth_db: AsyncSession = Depends(get_auth_db),
) -> Principal:
    token = _bearer_token(request)
    if token:
        principal = await load_principal_from_token(auth_db, token)
        if principal is not None:
            return principal
        raise HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        principal = await load_service_principal(auth_db, api_key)
        if principal is not None:
            return principal
        raise HTTPException(status_code=401, detail="服务凭据无效或已过期")
    raise HTTPException(
        status_code=401,
        detail="未提供认证令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_scope(scope: str):
    """业务路由统一使用本依赖：先确认身份，再检查数据库中的当前权限。"""

    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"缺少权限：{scope}")
        return principal

    return dependency


async def get_current_user(principal: Principal = Depends(get_principal)) -> dict:
    """向后兼容的 dict 形式的当前主体。"""
    return principal.as_dict()


async def record_auth_audit(
    session: AsyncSession,
    *,
    principal_type: str,
    principal_name: str,
    action: str,
    outcome: str,
    resource: str = "",
    client: str = "",
    detail: str = "",
) -> None:
    """写入认证/权限审计；审计失败不得影响主流程，且绝不记录凭据或原始查询。"""
    from backend.models.database_models import AuthAuditLog

    try:
        session.add(
            AuthAuditLog(
                principal_type=principal_type[:20],
                principal_name=principal_name[:100],
                action=action[:50],
                resource=resource[:255],
                outcome=outcome[:20],
                client_ip=client[:64],
                detail=detail[:500],
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - 审计失败不能反向中断业务
        await session.rollback()
        logger.warning("认证审计写入失败: %s", sanitize_error(exc))


if __name__ == "__main__":
    # 生成 bcrypt 哈希：
    #   python -m backend.utils.auth "你的明文密码"
    import sys

    if len(sys.argv) != 2:
        print("用法: python -m backend.utils.auth <明文密码>")
        raise SystemExit(1)
    print(hash_password(sys.argv[1]))
