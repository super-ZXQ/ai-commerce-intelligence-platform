import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from pydantic import BaseModel

from backend.config import get_settings

settings = get_settings()

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24


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
    return hashlib.sha256(f"{settings.jwt_secret}:{password}".encode()).hexdigest()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _hash_password(plain_password) == hashed_password


def create_access_token(username: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {"sub": username, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[TokenData]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        exp: int = payload.get("exp")
        if username is None:
            return None
        return TokenData(username=username, exp=exp)
    except JWTError:
        return None


DEFAULT_USERS = {
    "admin": _hash_password("admin123"),
    "analyst": _hash_password("analyst123"),
}


def authenticate_user(username: str, password: str) -> bool:
    stored_hash = DEFAULT_USERS.get(username)
    if not stored_hash:
        return False
    return verify_password(password, stored_hash)


def generate_api_key() -> str:
    return f"ea_{secrets.token_urlsafe(32)}"
