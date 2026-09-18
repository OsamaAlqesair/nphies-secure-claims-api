"""JWT authentication with Argon2id credentials and current database roles."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
import secrets

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, Query, Response, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from models import User
from services.audit import add_event
from schemas.claim import OperationOutcome

Role = Literal["admin", "provider"]
ALGORITHM = "HS256"  # Never accept the token's choice of algorithm.
LOGIN_FAILURE_LIMIT = 5
LOCKOUT_MINUTES = 15


@dataclass(frozen=True)
class AuthSettings:
    secret_key: str = field(repr=False)
    issuer: str
    audience: str
    lifetime_minutes: int

    @classmethod
    def from_file(cls, path: Path):
        values = dotenv_values(path)
        key = values.get("SECRET_KEY")
        if not key or len(key.encode("utf-8")) < 64:
            raise RuntimeError(
                "SECRET_KEY must be configured in .env with at least 64 bytes. No fallback is allowed."
            )
        issuer = values.get("JWT_ISSUER")
        audience = values.get("JWT_AUDIENCE")
        lifetime = int(values.get("ACCESS_TOKEN_MINUTES") or "15")
        if not issuer or not audience or not 1 <= lifetime <= 30:
            raise RuntimeError(
                "Configure JWT_ISSUER, JWT_AUDIENCE and a 1-30 minute token lifetime in .env."
            )
        return cls(key, issuer, audience, lifetime)


settings = AuthSettings.from_file(Path(__file__).with_name(".env"))
password_hasher = PasswordHash.recommended()
# A random, unusable dummy credential keeps unknown-user checks expensive too.
_dummy_hash = password_hasher.hash(secrets.token_urlsafe(48))
bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
    responses={
        status: {
            "description": "Authentication or validation failure",
            "content": {
                "application/fhir+json": {
                    "schema": OperationOutcome.model_json_schema()
                }
            },
        }
        for status in (401, 403, 422)
    },
)


class AuthError(Exception):
    def __init__(self, status_code: int = 401):
        self.status_code = status_code


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(
        min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"
    )
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: Role
    is_active: bool


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Passwords must contain 12-128 characters.")
    return password_hasher.hash(password)


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user.id),
            "iss": settings.issuer,
            "aud": settings.audience,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=settings.lifetime_minutes),
            "jti": secrets.token_urlsafe(24),
            "ver": user.token_version,
            "token_use": "access",
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )


def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError()
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.secret_key,
            algorithms=[ALGORITHM],
            issuer=settings.issuer,
            audience=settings.audience,
            options={
                "require": [
                    "sub",
                    "iss",
                    "aud",
                    "iat",
                    "nbf",
                    "exp",
                    "jti",
                    "ver",
                    "token_use",
                ]
            },
        )
        subject = claims["sub"]
        if (
            not isinstance(subject, str)
            or not subject.isascii()
            or not subject.isdigit()
            or len(subject) > 10
        ):
            raise AuthError()
        user_id = int(subject)
        if (
            not 1 <= user_id <= 2147483647
            or type(claims["ver"]) is not int
            or claims["token_use"] != "access"
        ):
            raise AuthError()
        if not isinstance(claims["jti"], str) or not claims["jti"]:
            raise AuthError()
    except (InvalidTokenError, ValueError, TypeError):
        raise AuthError() from None
    # Read current status/role every time; roles are never accepted from clients.
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active or user.token_version != claims["ver"]:
        raise AuthError()
    request.state.audit_actor_id = user.id
    return user


def require_roles(*roles: Role):
    def authorize(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise AuthError(403)
        return user

    return authorize


claim_access = require_roles("admin", "provider")
admin_access = require_roles("admin")


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
):
    # Row locks serialize failed-attempt counters across PostgreSQL workers.
    user = db.scalar(
        select(User).where(User.username == payload.username).with_for_update()
    )
    now = datetime.now(timezone.utc)
    locked_until = user.locked_until if user is not None else None
    if locked_until is not None and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)  # SQLite tests
    locked = locked_until is not None and locked_until > now
    usable = user is not None and user.is_active and not locked
    stored_hash = user.password_hash if usable else _dummy_hash
    try:
        valid, replacement = password_hasher.verify_and_update(
            payload.password.get_secret_value(), stored_hash
        )
    except UnknownHashError:
        password_hasher.verify(payload.password.get_secret_value(), _dummy_hash)
        valid, replacement = False, None
    if not usable or not valid:
        if user is not None and user.is_active and not locked:
            if locked_until is not None:
                user.failed_login_attempts = 0
                user.locked_until = None
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= LOGIN_FAILURE_LIMIT:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        add_event(
            db,
            request,
            "auth.login",
            "failure",
            "locked" if locked else "invalid_credentials",
            401,
            actor_id=user.id if user is not None else None,
        )
        db.commit()
        request.state.login_audited = True
        raise AuthError()  # Same response for missing, disabled, locked or wrong password.
    user.failed_login_attempts = 0
    user.locked_until = None
    if replacement:
        user.password_hash = replacement
    token = create_access_token(user)
    add_event(
        db, request, "auth.login", "success", "authenticated", 200, actor_id=user.id
    )
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(
        access_token=token,
        expires_in=settings.lifetime_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[User, Depends(current_user)]):
    return user


@router.get("/users", response_model=list[UserResponse])
def users(
    admin: Annotated[User, Depends(admin_access)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return db.scalars(select(User).order_by(User.id).offset(offset).limit(limit)).all()
