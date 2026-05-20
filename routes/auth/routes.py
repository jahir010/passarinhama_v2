from typing import Optional
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Form, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from applications.user.models import User, UserStatus
from app.token import (
    get_current_user,
    create_access_token,
    create_refresh_token,
    set_auth_cookies,
    blocklist_refresh_token,
    is_refresh_token_blocked,
    _normalize_token,
    REFRESH_TOKEN_EXPIRE_DAYS,
    REFRESH_SECRET_KEY,
    ALGORITHM,
)
from app.utils.otp_manager import generate_otp, verify_otp, verify_session_key
from app.config import settings

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def detect_input_type(value: str) -> str:
    """Validates the value is a well-formed email address."""
    value = value.strip()
    if re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', value):
        return "email"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid email address.",
    )


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _build_token_data(user: User) -> dict:
    """
    Build the JWT payload for a user.

    Kept lean on purpose — role permissions are always read fresh from the
    database via has_permission(), so stale role data in a token can never
    cause a privilege-escalation bug.
    """
    return {
        "sub":          str(user.id),
        "email":        user.email or "",
        "is_superuser": user.is_superuser,
    }


def _check_user_suspended(user: User) -> None:
    """
    Block suspended or deleted users from receiving tokens.

    PENDING users ARE allowed to authenticate — they just have limited access
    until an admin validates their account.  Only SUSPENDED / deleted accounts
    are hard-blocked at login time.
    """
    if user.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deleted. Please contact support.",
        )
    if user.status == UserStatus.SUSPENDED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Please contact support.",
        )


class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str


async def _issue_auth_tokens(user: User, response: Response | None = None) -> dict:
    """Create a token pair, optionally set cookies, and update login timestamps."""
    token_data    = _build_token_data(user)
    access_token  = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)

    if response is not None:
        set_auth_cookies(response, access_token, refresh_token)

    now = datetime.now(timezone.utc)
    await User.filter(id=user.id).update(last_login_at=now, last_seen_at=now)

    return {
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "token_type":    "bearer",
    }


async def _get_role_slug(user: User) -> str | None:
    """Safely fetch the slug of the user's role without crashing on un-prefetched FK."""
    try:
        if user.role_id is None:
            return None
        role = await user.role
        return role.slug if role else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN (OAuth2 / Swagger)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/login_auth2", response_model=TokenResponse)
async def login_auth2(
    response:  Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Standard OAuth2 password flow used by Swagger UI."""
    email = _normalize_email(form_data.username)
    await detect_input_type(email)

    user = await User.get_or_none(email=email)
    if not user or not user.verify_password(form_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _check_user_suspended(user)
    return await _issue_auth_tokens(user, response)


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN WITH OTP SUPPORT
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(
    response:  Response,
    email:     str           = Form(...),
    password:  str           = Form(...),
    otp_value: Optional[str] = Form(None),
):
    """
    Full login endpoint with optional 2FA / OTP gate.

    Flow:
      1. Validate credentials.
      2. If 2FA is enabled and no OTP supplied → generate & send OTP,
         return otp_required so the client can prompt for the code.
      3. If OTP is supplied → verify it, then issue tokens.
      4. If 2FA is disabled → issue tokens immediately.
    """
    email = _normalize_email(email)
    await detect_input_type(email)

    user = await User.get_or_none(email=email)
    if not user or not user.verify_password(password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    _check_user_suspended(user)

    # ── 2FA gate ─────────────────────────────────────────────────────────
    if getattr(user, "is_active_2fa", False):
        normalized_otp = otp_value.strip() if otp_value else None
        if not normalized_otp:
            otp = await generate_otp(email, "login")
            return {
                "status":  "otp_required",
                "message": (
                    f"OTP sent to {email}"
                    + (f" (DEBUG: {otp})" if settings.DEBUG else "")
                ),
                "purpose": "login",
            }
        await verify_otp(email, normalized_otp, "login")

    token_response = await _issue_auth_tokens(user, response)
    token_response["role_slug"] = await _get_role_slug(user)
    token_response["status"]    = user.status
    return token_response


# ─────────────────────────────────────────────────────────────────────────────
# REFRESH TOKEN
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token_endpoint(
    request:       Request,
    response:      Response,
    refresh_token: Optional[str] = Form(None),
):
    """
    Issues a new access + refresh token pair from a valid, non-blocklisted
    refresh token.  The consumed refresh token is immediately blocklisted
    (rotation), so replay attacks are detected.

    Accepts the token via form body OR the HTTP-only cookie.
    """
    from jose import jwt, JWTError, ExpiredSignatureError
    from tortoise.exceptions import DoesNotExist

    raw = _normalize_token(refresh_token or request.cookies.get("refresh_token"))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token required.",
        )

    try:
        payload = jwt.decode(raw, REFRESH_SECRET_KEY, algorithms=[ALGORITHM])
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired. Please log in again.",
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token.",
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type.",
        )

    jti = payload.get("jti")
    if jti and await is_refresh_token_blocked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked. Please log in again.",
        )

    try:
        user = await User.get(id=payload.get("sub"))
    except DoesNotExist:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )

    _check_user_suspended(user)

    # Blocklist the consumed token before issuing a replacement (rotation)
    if jti:
        await blocklist_refresh_token(jti, int(REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60))

    return await _issue_auth_tokens(user, response)


# ─────────────────────────────────────────────────────────────────────────────
# SEND OTP
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/send_otp")
async def send_otp(
    email:   str = Form(...),
    purpose: str = Form("signup", description="signup | forgot_password | login"),
):
    email   = _normalize_email(email)
    await detect_input_type(email)
    purpose = purpose.strip().lower()

    allowed_purposes = {"signup", "forgot_password", "login"}
    if purpose not in allowed_purposes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid OTP purpose. Must be one of: {', '.join(sorted(allowed_purposes))}.",
        )

    user = await User.get_or_none(email=email)

    if purpose == "signup" and user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered.",
        )
    if purpose in {"forgot_password", "login"} and not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email address.",
        )
    if purpose == "login" and user and not getattr(user, "is_active_2fa", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP login is not enabled for this account.",
        )

    otp = await generate_otp(email, purpose)

    return {
        "status":  "success",
        "message": (
            f"OTP sent to {email}"
            + (f" (DEBUG: {otp})" if settings.DEBUG else "")
        ),
        "purpose": purpose,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNUP
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/signup")
async def signup(
    response:   Response,
    first_name: str = Form(...),
    last_name:  str = Form(...),
    email:      str = Form(...),
    password:   str = Form(...),
    otp_value:  str = Form(...),
):
    """
    Self-service registration.

    Flow: verify OTP → create user with PENDING status → issue tokens.
    The account stays PENDING until an admin validates payment, at which
    point status is promoted to ACTIVE.

    Role is intentionally NOT accepted from the client on signup — admins
    assign roles via the PATCH /users/{id} endpoint.
    """
    email      = _normalize_email(email)
    await detect_input_type(email)
    first_name = first_name.strip()
    last_name  = last_name.strip()
    password   = password.strip()
    otp_value  = otp_value.strip()

    if not first_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="First name is required.")
    if not last_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Last name is required.")
    if not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password is required.")

    await verify_otp(email, otp_value, "signup")

    if await User.get_or_none(email=email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered.",
        )

    user = await User.create(
        first_name=first_name,
        last_name=last_name,
        email=email,
        password=User.set_password(password),
        # role is NULL at signup — assigned by admin
        status=UserStatus.PENDING,
    )

    token_response = await _issue_auth_tokens(user, response)
    return {
        "message": "Account created successfully. Awaiting admin validation.",
        **token_response,
        "status":  user.status,
    }


# ─────────────────────────────────────────────────────────────────────────────
# VERIFY OTP
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/verify_otp")
async def verify_otp_route(
    email:     str = Form(...),
    otp_value: str = Form(...),
    purpose:   str = Form(...),
):
    email = _normalize_email(email)
    await detect_input_type(email)
    session_key = await verify_otp(email, otp_value, purpose)
    return {
        "status":      "success",
        "session_key": session_key,
    }


# ─────────────────────────────────────────────────────────────────────────────
# RESET PASSWORD  (logged-in user — knows current password)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/reset_password")
async def reset_password(
    user:         User = Depends(get_current_user),
    old_password: str  = Form(...),
    new_password: str  = Form(...),
):
    new_password = new_password.strip()

    if not user.verify_password(old_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    if not new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password is required.",
        )
    if old_password == new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current password.",
        )

    user.password = User.set_password(new_password)
    await user.save(update_fields=["password"])
    return {"message": "Password updated successfully."}


# ─────────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD  (unauthenticated — uses OTP session key)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/forgot_password")
async def forgot_password(
    email:        str = Form(...),
    new_password: str = Form(...),
    session_key:  str = Form(...),
):
    """
    Reset password without being logged in.

    Requires a session_key obtained from POST /verify_otp with
    purpose=forgot_password.
    """
    email        = _normalize_email(email)
    await detect_input_type(email)
    new_password = new_password.strip()
    session_key  = session_key.strip()

    if not new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password is required.",
        )

    user = await User.get_or_none(email=email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account found with this email address.",
        )

    await verify_session_key(email, session_key, "forgot_password")

    user.password = User.set_password(new_password)
    await user.save(update_fields=["password"])
    return {"message": "Password reset successfully."}


# ─────────────────────────────────────────────────────────────────────────────
# VERIFY TOKEN
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/verify-token")
async def verify_token(
    request: Request,
    user:    User = Depends(get_current_user),
):
    """
    Confirm a token is valid and return the user's public profile.

    When the access token was silently rotated via a refresh token, the new
    token pair is returned under `new_tokens` so the client can store them.
    """
    response_data = {
        "id":           str(user.id),
        "email":        user.email,
        "first_name":   user.first_name,
        "last_name":    user.last_name,
        "role_slug":    await _get_role_slug(user),
        "status":       user.status,
        "is_superuser": user.is_superuser,
        "avatar_url":   user.avatar_url,
    }
    if hasattr(request.state, "new_tokens"):
        response_data["new_tokens"] = request.state.new_tokens
    return response_data