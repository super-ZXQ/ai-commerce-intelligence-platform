import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel

from backend.config import get_settings

settings = get_settings()

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


DEFAULT_USERS: dict[str, str] = {
    "admin": "$2b$12$Xq5inXxXtfU6.hqkK6sDveN7hkur1KxNDkW4/IFcADscx4wgGWZw6",
    "analyst": "$2b$12$WQWnIBiiUtfIHJwm1X.RaOdTaA3ZszhRQXHQLVVxrHzhaGzye5sdu",
}


def authenticate_user(username: str, password: str) -> bool:
    stored_hash = DEFAULT_USERS.get(username)
    if not stored_hash:
        return False
    return verify_password(password, stored_hash)


def generate_api_key() -> str:
    return f"ea_{secrets.token_urlsafe(32)}"
