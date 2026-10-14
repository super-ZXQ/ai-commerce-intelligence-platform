import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import get_settings
from backend.utils.auth import (
    TokenResponse,
    UserCredentials,
    authenticate_user,
    create_access_token,
    decode_token,
)

logger = logging.getLogger(__name__)
settings = get_settings()

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api/auth", tags=["认证"])


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌", headers={"WWW-Authenticate": "Bearer"})
    token_data = decode_token(credentials.credentials)
    if token_data is None:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期", headers={"WWW-Authenticate": "Bearer"})
    return {"username": token_data.username}


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(credentials: UserCredentials, response: Response) -> TokenResponse:
    if not authenticate_user(credentials.username, credentials.password):
        logger.warning(f"登录失败: 用户 {credentials.username}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    access_token = create_access_token(credentials.username)
    # 页面使用 HttpOnly 会话；业务 API 仍只接受 Bearer，避免引入 Cookie 写操作 CSRF。
    response.set_cookie(
        "ea_session", access_token, httponly=True, samesite="strict",
        secure=not settings.debug, max_age=settings.jwt_expire_minutes * 60,
    )
    logger.info(f"登录成功: {credentials.username}")
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.get("/page-session", summary="网关及页面服务端认证")
async def page_session(request: Request) -> dict:
    token = request.cookies.get("ea_session", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:]
    data = decode_token(token)
    if data is None:
        raise HTTPException(status_code=401, detail="请先在 /demo 登录；生产环境需 HTTPS")
    return {"username": data.username, "authenticated": True}


@router.post("/refresh", response_model=TokenResponse, summary="刷新令牌")
async def refresh_token(user: dict = Depends(get_current_user)) -> TokenResponse:
    new_token = create_access_token(user["username"])
    return TokenResponse(
        access_token=new_token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.get("/me", summary="获取当前用户信息")
async def get_me(user: dict = Depends(get_current_user)) -> dict:
    return {"username": user["username"], "authenticated": True}
