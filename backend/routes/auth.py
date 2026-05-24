import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.utils.auth import (
    UserCredentials,
    TokenResponse,
    create_access_token,
    decode_token,
    authenticate_user,
)

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api/auth", tags=["认证"])


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌", headers={"WWW-Authenticate": "Bearer"})
    token_data = decode_token(credentials.credentials)
    if token_data is None:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期", headers={"WWW-Authenticate": "Bearer"})
    return {"username": token_data.username}


def optional_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    if credentials is None:
        return None
    token_data = decode_token(credentials.credentials)
    if token_data is None:
        raise HTTPException(status_code=401, detail="认证令牌无效")
    return {"username": token_data.username}


@router.post("/login", response_model=TokenResponse, summary="用户登录")
async def login(credentials: UserCredentials) -> TokenResponse:
    if not authenticate_user(credentials.username, credentials.password):
        logger.warning(f"登录失败: 用户 {credentials.username}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    access_token = create_access_token(credentials.username)
    logger.info(f"登录成功: {credentials.username}")
    return TokenResponse(access_token=access_token, expires_in=60 * 60 * 24)


@router.post("/refresh", response_model=TokenResponse, summary="刷新令牌")
async def refresh_token(user: dict = Depends(get_current_user)) -> TokenResponse:
    new_token = create_access_token(user["username"])
    return TokenResponse(access_token=new_token, expires_in=60 * 60 * 24)


@router.get("/me", summary="获取当前用户信息")
async def get_me(user: dict = Depends(get_current_user)) -> dict:
    return {"username": user["username"], "authenticated": True}
