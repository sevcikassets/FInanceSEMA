from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os

import jwt
import pyotp
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

security = HTTPBearer(auto_error=False)
ALL_AGENDAS = [
    "assets",
    "costs",
    "categories",
    "loans",
    "transactions",
    "watchlist",
    "stats",
    "portfolio",
    "history",
    "alerts",
    "charts",
    "rates",
    "users",
    "subjects",
]

# "rates" (shared CNB exchange-rate history), "users" (user management) and
# "subjects" (Subjekt/Portfolio management) apply app-wide and stay governed
# by AppUser.allowed_agendas. Every other agenda is scoped per-Subjekt
# (Portfolio) via PortfolioAccess.allowed_agendas instead - see
# require_portfolio_access in main.py.
GLOBAL_AGENDAS = ["rates", "users", "subjects"]
PORTFOLIO_SCOPED_AGENDAS = [agenda for agenda in ALL_AGENDAS if agenda not in GLOBAL_AGENDAS]

# Marks a token issued after username+password succeed but before the TOTP
# code is verified. It can only be redeemed at /auth/2fa/login - require_user
# explicitly refuses it, so a stolen/leaked pending token never grants access
# to any real endpoint on its own.
PENDING_2FA_SCOPE = "2fa-pending"


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt, digest = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hash_password(password, salt).split("$", 2)[2]
    return hmac.compare_digest(candidate, digest)


def create_token(username: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=12)).timestamp()),
    }
    return jwt.encode(payload, settings.app_token_secret, algorithm="HS256")


def create_pending_2fa_token(username: str, settings: Settings | None = None) -> str:
    """Short-lived token proving username+password already checked out, used
    only to redeem the second factor at /auth/2fa/login. 5 minutes is enough
    to type a 6-digit code without leaving a long-lived half-authenticated
    token lying around in the browser."""
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "scope": PENDING_2FA_SCOPE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return jwt.encode(payload, settings.app_token_secret, algorithm="HS256")


def verify_pending_2fa_token(token: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(token, settings.app_token_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Přihlášení vypršelo, zkuste to znovu") from exc
    if payload.get("scope") != PENDING_2FA_SCOPE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Neplatný token")
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Neplatný token")
    return username


def verify_totp_code(secret: str, code: str) -> bool:
    code = (code or "").strip()
    if not code:
        return False
    # valid_window=1 tolerates the code from the previous/next 30s step, so a
    # slightly slow typist or minor clock drift doesn't get rejected.
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def authenticate(username: str, password: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return username == settings.app_username and password == settings.app_password


def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    settings: Settings = Depends(get_settings),
) -> str:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = jwt.decode(credentials.credentials, settings.app_token_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if payload.get("scope") == PENDING_2FA_SCOPE:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="2FA verification required")
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return username
