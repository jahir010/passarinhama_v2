import re
import secrets
from hmac import compare_digest

from fastapi import HTTPException, status
from fastapi.templating import Jinja2Templates
from redis.exceptions import RedisError

from app.config import settings
from app.redis import get_redis
from app.utils.send_email import send_email

templates = Jinja2Templates(directory="templates")

OTP_EXPIRY_SECONDS        = 60 * 5          # 5 minutes
SESSION_KEY_EXPIRY_SECONDS = OTP_EXPIRY_SECONDS
MAX_ATTEMPTS_PER_HOUR     = 20
EMAIL_REGEX               = r"^[\w\.-]+@[\w\.-]+\.\w+$"

PURPOSE_MESSAGES = {
    "login": (
        "Login Verification",
        "Use the OTP below to log in to your account.",
    ),
    "signup": (
        "Verify Your Email",
        "Use the OTP below to verify your email address and complete signup.",
    ),
    "forgot_password": (
        "Reset Your Password",
        "Use the OTP below to reset your password.",
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal normalisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def detect_input_type(value: str) -> str:
    normalized = value.strip().lower()
    if re.match(EMAIL_REGEX, normalized):
        return "email"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid email address.",
    )


def _normalize_user_key(user_key: str) -> str:
    normalized = user_key.strip().lower()
    detect_input_type(normalized)
    return normalized


def _normalize_purpose(purpose: str) -> str:
    normalized = purpose.strip().lower()
    if normalized not in PURPOSE_MESSAGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP purpose",
        )
    return normalized


def _normalize_otp_value(otp_value: str) -> str:
    normalized = otp_value.strip()
    if not re.fullmatch(r"\d{6}", normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP.",
        )
    return normalized


def _normalize_session_key(session_key: str) -> str:
    normalized = session_key.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session key.",
        )
    return normalized


# ─────────────────────────────────────────────────────────────────────────────
# Redis key builders
# ─────────────────────────────────────────────────────────────────────────────

def _otp_key(user_key: str, purpose: str) -> str:
    return f"{purpose}:otp:{user_key}"


def _otp_attempts_key(user_key: str, purpose: str) -> str:
    return f"{purpose}:otp_attempts:{user_key}"


def _session_key(user_key: str, purpose: str) -> str:
    return f"{purpose}:session:{user_key}"


def _get_redis_client():
    try:
        return get_redis()
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OTP service is unavailable.",
        )


def _raise_otp_service_unavailable() -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="OTP service is unavailable.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def generate_otp(user_key: str, purpose: str) -> str:
    """
    Generate a 6-digit OTP, send it to the user's email, and persist it in
    Redis with a TTL.  Returns the OTP string (useful in DEBUG mode).
    """
    normalized_user_key = _normalize_user_key(user_key)
    normalized_purpose  = _normalize_purpose(purpose)
    redis               = _get_redis_client()

    otp_key      = _otp_key(normalized_user_key, normalized_purpose)
    attempts_key = _otp_attempts_key(normalized_user_key, normalized_purpose)
    session_key  = _session_key(normalized_user_key, normalized_purpose)

    try:
        attempts_raw = await redis.get(attempts_key)
    except RedisError:
        _raise_otp_service_unavailable()
    attempts     = int(attempts_raw) if attempts_raw else 0
    if attempts >= MAX_ATTEMPTS_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests. Try again later.",
        )

    otp             = f"{secrets.randbelow(900000) + 100000:06d}"
    title, message  = PURPOSE_MESSAGES[normalized_purpose]

    html_message = templates.get_template("otp_email.html").render(
        {
            "title":      title,
            "name":       normalized_user_key,
            "otp":        otp,
            "expires_in": OTP_EXPIRY_SECONDS // 60,
            "message":    message,
        }
    )

    if settings.DEBUG:
        print(
            f"[DEBUG] OTP for {normalized_user_key} ({normalized_purpose}): {otp}",
            flush=True,
        )
    else:
        try:
            await send_email(
                subject=title,
                message=f"Your OTP is: {otp}",
                html_message=html_message,
                to_email=normalized_user_key,
                retries=3,
                delay=2,
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send OTP.",
            )

    try:
        await redis.set(otp_key, otp, ex=OTP_EXPIRY_SECONDS)
        # Invalidate any existing session key so the old one cannot be reused
        await redis.delete(session_key)

        count = await redis.incr(attempts_key)
        if count == 1:
            await redis.expire(attempts_key, 3600)
    except RedisError:
        _raise_otp_service_unavailable()

    return otp


async def verify_otp(user_key: str, otp_value: str, purpose: str) -> str:
    """
    Verify the supplied OTP.  On success, deletes the OTP from Redis and
    returns a short-lived session key the caller can use for the next step
    (e.g. forgot_password).
    """
    normalized_user_key = _normalize_user_key(user_key)
    normalized_otp      = _normalize_otp_value(otp_value)
    normalized_purpose  = _normalize_purpose(purpose)
    redis               = _get_redis_client()

    otp_key          = _otp_key(normalized_user_key, normalized_purpose)
    session_key_name = _session_key(normalized_user_key, normalized_purpose)

    try:
        stored_otp = await redis.get(otp_key)
    except RedisError:
        _raise_otp_service_unavailable()
    if not stored_otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP expired or not found.",
        )

    if not compare_digest(stored_otp, normalized_otp):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP.",
        )

    session_key = secrets.token_urlsafe(32)
    try:
        await redis.delete(otp_key)
        await redis.set(session_key_name, session_key, ex=SESSION_KEY_EXPIRY_SECONDS)
    except RedisError:
        _raise_otp_service_unavailable()
    return session_key


async def verify_session_key(user_key: str, session_key: str, purpose: str) -> bool:
    """
    Verify the session key issued by verify_otp.  Deletes it on success
    so it is single-use.
    """
    normalized_user_key    = _normalize_user_key(user_key)
    normalized_session_key = _normalize_session_key(session_key)
    normalized_purpose     = _normalize_purpose(purpose)
    redis                  = _get_redis_client()

    redis_session_key   = _session_key(normalized_user_key, normalized_purpose)
    try:
        stored_session_key  = await redis.get(redis_session_key)
    except RedisError:
        _raise_otp_service_unavailable()

    if not stored_session_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired session key.",
        )

    if not compare_digest(stored_session_key, normalized_session_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session key.",
        )

    try:
        await redis.delete(redis_session_key)
    except RedisError:
        _raise_otp_service_unavailable()
    return True
