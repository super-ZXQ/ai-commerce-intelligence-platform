import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.database import get_auth_db
from backend.models.database_models import AuthUser
from backend.utils.auth import (
    Principal,
    TokenResponse,
    UserCredentials,
    authenticate_user,
    client_ip,
    create_access_token,
    effective_scopes,
    get_principal,
    record_auth_audit,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(
    credentials: UserCredentials,
    request: Request,
    response: Response,
    auth_db: AsyncSession = Depends(get_auth_db),
) -> TokenResponse:
    """校验数据库身份并同时下发 HttpOnly 会话 Cookie。

    浏览器统一使用 Cookie（Nginx auth_request 保护 /ai/、/BI/ 与 WebSocket）；
    API 客户端仍可使用返回的 Bearer Token。两者身份来源相同。
    """
    user = await authenticate_user(auth_db, credentials.username, credentials.password)
    ip = client_ip(request)
    if user is None:
        await record_auth_audit(
            auth_db,
            principal_type="user",
            principal_name=credentials.username,
            action="login",
            outcome="denied",
            client=ip,
            detail="用户名或密码错误",
        )
        logger.warning("登录失败: 用户 %s", credentials.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    access_token = create_access_token(
        user.username,
        role=user.role,
        # JWT 只用于标识会话；鉴权每次读库，角色默认权限与显式权限取并集。
        scopes=sorted(effective_scopes(user.role, user.scopes)),
        token_version=int(user.token_version),
    )
    _set_session_cookie(response, access_token)
    response.headers["Cache-Control"] = "no-store"
    await record_auth_audit(
        auth_db,
        principal_type="user",
        principal_name=user.username,
        action="login",
        outcome="success",
        client=ip,
    )
    logger.info("登录成功: %s", user.username)
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/logout", summary="退出登录")
async def logout(
    request: Request,
    response: Response,
    auth_db: AsyncSession = Depends(get_auth_db),
) -> dict:
    """清除浏览器会话 Cookie；Bearer Token 客户端自行丢弃令牌。"""
    principal: Principal | None = None
    try:
        principal = await get_principal(request, auth_db)
    except HTTPException:
        principal = None
    response.delete_cookie(settings.auth_cookie_name, path="/")
    if principal is not None:
        await record_auth_audit(
            auth_db,
            principal_type=principal.principal_type,
            principal_name=principal.name,
            action="logout",
            outcome="success",
            client=client_ip(request),
        )
    return {"status": "ok"}


@router.get("/verify", summary="Nginx auth_request 会话校验")
async def verify(response: Response, principal: Principal = Depends(get_principal)) -> dict:
    """供 Nginx 子请求与前端状态检查使用；不返回任何凭据。"""
    response.headers["X-Auth-User"] = principal.name
    response.headers["X-Auth-Role"] = principal.role or principal.principal_type
    response.headers["X-Auth-Scopes"] = ",".join(sorted(principal.scopes))
    response.headers["Cache-Control"] = "no-store"
    return {
        "authenticated": True,
        "username": principal.name,
        "role": principal.role,
        "scopes": sorted(principal.scopes),
        "principal_type": principal.principal_type,
    }


@router.post("/refresh", response_model=TokenResponse, summary="刷新令牌")
async def refresh_token(
    response: Response,
    principal: Principal = Depends(get_principal),
    auth_db: AsyncSession = Depends(get_auth_db),
) -> TokenResponse:
    if principal.principal_type != "user":
        raise HTTPException(status_code=400, detail="服务凭据不能刷新会话")
    user = (
        await auth_db.execute(select(AuthUser).where(AuthUser.username == principal.name))
    ).scalar_one_or_none()
    if user is None or not bool(user.is_active):
        raise HTTPException(status_code=401, detail="账号不可用")
    new_token = create_access_token(
        user.username,
        role=user.role,
        scopes=sorted(effective_scopes(user.role, user.scopes)),
        token_version=int(user.token_version),
    )
    _set_session_cookie(response, new_token)
    return TokenResponse(
        access_token=new_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.get("/me", summary="获取当前用户信息")
async def get_me(principal: Principal = Depends(get_principal)) -> dict:
    return {
        "username": principal.name,
        "role": principal.role,
        "scopes": sorted(principal.scopes),
        "principal_type": principal.principal_type,
        "authenticated": True,
    }
