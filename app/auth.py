"""Autenticacao local, independente de provedores externos.

Administradores sao provisionados via `python -m app.local_users`.
Novos usuarios podem se cadastrar no site, quando habilitado.
Senhas usam PBKDF2-HMAC-SHA256 (salt exclusivo); tokens temporarios sao
assinados com HMAC-SHA256 e invalidados quando a senha muda.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import AppUser

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)
_logger = logging.getLogger(__name__)
_HASH_ITERATIONS = 600_000
_TOKEN_TTL_SECONDS = 12 * 60 * 60
_failure_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_signup_attempts: dict[str, list[float]] = {}


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    is_admin: bool
    plan: str


class LoginInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class SignupInput(BaseModel):
    # Campos como is_admin, plan, id e owner_id sao proibidos no cadastro.
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=1024)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("A senha precisa ter pelo menos 12 caracteres")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS)
    return f"pbkdf2_sha256${_HASH_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        rounds = int(iterations)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= rounds <= 2_000_000:
            return False
        salt, expected = _unb64(salt_b64), _unb64(digest_b64)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _secret() -> bytes:
    value = (get_settings().local_session_secret or "").strip()
    if len(value.encode("utf-8")) < 32:
        raise RuntimeError("LOCAL_SESSION_SECRET deve ter pelo menos 32 caracteres aleatorios")
    return value.encode("utf-8")


def _password_version(stored: str) -> str:
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:24]


def _sign_token(user: AppUser) -> str:
    now = int(time.time())
    payload = {
        "sub": user.id,
        "iat": now,
        "exp": now + _TOKEN_TTL_SECONDS,
        "pw": _password_version(user.password_hash),
    }
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(_secret(), f"v1.{body}".encode("ascii"), hashlib.sha256).digest())
    return f"v1.{body}.{signature}"


def _verify_token(token: str) -> dict | None:
    if not token or len(token) > 4096:
        return None
    try:
        version, body, signature = token.split(".")
        if version != "v1":
            return None
        expected = hmac.new(_secret(), f"{version}.{body}".encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            return None
        payload = json.loads(_unb64(body))
        if not isinstance(payload, dict):
            return None
        now = int(time.time())
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            return None
        if not isinstance(payload.get("iat"), int) or not isinstance(payload.get("exp"), int):
            return None
        if payload["iat"] > now + 60 or payload["exp"] <= now or payload["exp"] - payload["iat"] != _TOKEN_TTL_SECONDS:
            return None
        if not isinstance(payload.get("pw"), str):
            return None
        return payload
    except (ValueError, TypeError, UnicodeError, KeyError):
        return None


def _request_key(request: Request) -> str:
    # Nao confiar em X-Forwarded-For informado pelo proprio cliente.
    return request.client.host if request.client else "unknown"


def _check_rate_limit(key: str) -> None:
    now = time.monotonic()
    with _failure_lock:
        times = [x for x in _failures.get(key, []) if now - x < 600]
        _failures[key] = times
        if len(times) >= 12:
            raise HTTPException(429, "Muitas tentativas de login. Tente novamente mais tarde.")


def _register_failure(key: str) -> None:
    now = time.monotonic()
    with _failure_lock:
        _failures[key] = [x for x in _failures.get(key, []) if now - x < 600] + [now]
        if len(_failures) > 20_000:
            _failures.clear()


def _check_signup_rate_limit(key: str) -> None:
    # Contabiliza todas as tentativas (inclusive as rejeitadas por email duplicado).
    # Single worker no deploy Linux; sempre limitar rede no proxy/firewall.
    now = time.monotonic()
    max_attempts = get_settings().local_signup_max_per_hour
    with _failure_lock:
        recent = [t for t in _signup_attempts.get(key, []) if now - t < 3600]
        if len(recent) >= max_attempts:
            _signup_attempts[key] = recent
            raise HTTPException(429, "Limite de cadastros atingido. Tente novamente mais tarde.")
        _signup_attempts[key] = [*recent, now]
        if len(_signup_attempts) > 20000:
            _signup_attempts.clear()


def _normalized_email(email: str) -> str:
    value = email.strip().casefold()
    # O SQLite usa NOCASE apenas para ASCII; nao permitir identidades com
    # normalizacao ambigua nas versoes do cliente/banco.
    if not value.isascii() or len(value) > 320 or not re.fullmatch(
        r"[^\s@]{1,64}@[^\s@]+\.[^\s@]{2,}", value
    ):
        raise HTTPException(422, "Informe um e-mail valido")
    return value


_SIGNUP_RETRY_DELAYS = (0.25, 0.75)


def _is_sqlite_busy(exc: OperationalError) -> bool:
    """Identifica apenas SQLITE_BUSY/SQLITE_LOCKED, inclusive codigos estendidos."""
    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    # Versoes antigas do sqlite3 nao expunham sqlite_errorcode.
    message = str(original).casefold()
    return "database is locked" in message or "database table is locked" in message


@router.post("/signup", status_code=201)
def signup(payload: SignupInput, request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if settings.local_auth_bypass:
        raise HTTPException(403, "Cadastro indisponivel no modo de desenvolvimento")
    if not settings.local_signup_enabled:
        raise HTTPException(403, "Novos cadastros estao desativados")
    try:
        _secret()
    except RuntimeError as exc:
        _logger.error("Cadastro local sem chave de sessao configurada: %s", exc)
        raise HTTPException(503, "Autenticacao do servidor nao configurada") from exc

    _check_signup_rate_limit(_request_key(request))
    email = _normalized_email(payload.email)
    # Hash antes da primeira leitura no SQLite: nao deixar uma transacao
    # aberta durante o PBKDF2, que pode levar centenas de milissegundos.
    encoded_password = hash_password(payload.password)
    user = AppUser(
        id=uuid4().hex,
        email=email,
        password_hash=encoded_password,
        is_admin=False,
        plan="FREE",
    )
    for attempt in range(len(_SIGNUP_RETRY_DELAYS) + 1):
        try:
            existing = db.scalar(
                select(AppUser.id).where(func.lower(func.trim(AppUser.email)) == email)
            )
            if existing is not None:
                raise HTTPException(409, "Este e-mail ja possui cadastro")
            db.add(user)
            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            # A protecao UNIQUE tambem impede emails duplicados quando
            # duas pessoas se registram ao mesmo tempo.
            raise HTTPException(409, "Este e-mail ja possui cadastro") from exc
        except OperationalError as exc:
            db.rollback()
            if not _is_sqlite_busy(exc):
                raise
            # SQLAlchemy nao permite reutilizar a Session sem rollback.
            # O ORM mantem o mesmo identificador para novas tentativas.
            if attempt == len(_SIGNUP_RETRY_DELAYS):
                _logger.warning(
                    "Cadastro indisponivel por concorrencia de escrita SQLite "
                    "apos %s tentativa(s); solicite nova tentativa mais tarde",
                    attempt + 1,
                )
                raise HTTPException(
                    status_code=503,
                    detail="O banco esta ocupado processando outra demanda. "
                           "Tente criar a conta novamente em instantes.",
                    headers={"Retry-After": "3"},
                ) from exc
            time.sleep(_SIGNUP_RETRY_DELAYS[attempt])
    # A resposta so e emitida depois do commit confirmado.
    return {
        "access_token": _sign_token(user),
        "token_type": "bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "email": user.email,
        "plan": user.plan,
    }


@router.post("/login")
def login(payload: LoginInput, request: Request, db: Session = Depends(get_db)):
    if get_settings().local_auth_bypass:
        raise HTTPException(403, "Login desativado no modo de desenvolvimento")
    try:
        _secret()
    except RuntimeError as exc:
        _logger.error("Login local nao configurado: %s", exc)
        raise HTTPException(503, "Autenticacao do servidor nao configurada") from exc

    key = _request_key(request)
    _check_rate_limit(key)
    email = payload.email.strip().casefold()
    user = db.scalar(select(AppUser).where(func.lower(AppUser.email) == email))
    if not user:
        # Reduz diferencas de tempo entre usuario inexistente e senha incorreta.
        hashlib.pbkdf2_hmac("sha256", payload.password.encode("utf-8"), b"0" * 16, _HASH_ITERATIONS)
    if not user or not verify_password(payload.password, user.password_hash):
        _register_failure(key)
        raise HTTPException(401, "Email ou senha invalidos")
    with _failure_lock:
        _failures.pop(key, None)

    return {
        "access_token": _sign_token(user),
        "token_type": "bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "email": user.email,
    }


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AuthUser:
    settings = get_settings()
    if settings.local_auth_bypass:
        if settings.app_env.casefold() == "production":
            raise HTTPException(503, "LOCAL_AUTH_BYPASS e proibido em producao")
        row = db.get(AppUser, "local-development")
        if row is None:
            row = AppUser(
                id="local-development",
                email="local@development.invalid",
                plan="INSTITUCIONAL",
                is_admin=True,
            )
            db.add(row)
            db.commit()
        return AuthUser(id=row.id, email=row.email, is_admin=True, plan="INSTITUCIONAL")

    if credentials is None or not credentials.credentials:
        raise HTTPException(401, "Autenticacao necessaria")
    try:
        token = _verify_token(credentials.credentials)
    except RuntimeError:
        raise HTTPException(503, "Autenticacao nao configurada")
    if token is None:
        raise HTTPException(401, "Sessao invalida ou expirada")
    row = db.get(AppUser, token["sub"])
    if row is None or not row.password_hash or not hmac.compare_digest(
        token["pw"], _password_version(row.password_hash)
    ):
        raise HTTPException(401, "Sessao invalida ou expirada")
    return AuthUser(id=row.id, email=row.email, is_admin=bool(row.is_admin), plan=row.plan or "FREE")


@router.get("/me")
def auth_me(user: AuthUser = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "is_admin": user.is_admin, "plan": user.plan}


def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(403, "Acesso administrativo necessario")
    return user
