from fastapi import Depends, Header, HTTPException, Request, status
from .token import get_current_user, oauth2_scheme
from applications.user.models import FEATURES, User, UserStatus


# ─────────────────────────────────────────────────────────────────────────────
# Basic guards
# ─────────────────────────────────────────────────────────────────────────────

async def superuser_required(current_user: User = Depends(get_current_user)) -> User:
    """Allows only superusers."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser access required.",
        )
    return current_user


async def login_required(current_user: User = Depends(get_current_user)) -> User:
    """Any authenticated, active user."""
    return current_user


# ─────────────────────────────────────────────────────────────────────────────
# Optional user  (returns None when unauthenticated instead of raising 401)
# ─────────────────────────────────────────────────────────────────────────────

async def get_optional_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    refresh_token: str | None = Header(default=None, alias="refresh-token"),
) -> User | None:
    """
    Soft dependency: returns the authenticated User or None.
    Useful for endpoints that serve different content to guests vs. members.
    """
    try:
        return await get_current_user(
            request=request,
            token=token,
            refresh_token=refresh_token,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Role-slug guard factory
#
# Roles are now fully dynamic (created in the database), so we match by slug.
# Superusers always pass unless allow_superuser=False.
# ─────────────────────────────────────────────────────────────────────────────

def role_required(*slugs: str, allow_superuser: bool = True):
    """
    Restrict an endpoint to users whose role slug is one of *slugs*.

    Example:
        @router.get("/admin")
        async def admin_view(user = Depends(role_required("admin", "moderator"))):
            ...
    """
    async def wrapper(current_user: User = Depends(get_current_user)) -> User:
        if allow_superuser and current_user.is_superuser:
            return current_user

        # Fetch role if not already prefetched
        try:
            role = await current_user.role
        except Exception:
            role = None

        if role is None or role.slug not in slugs:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have the required role for this action.",
            )
        return current_user

    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Feature-permission dependency factory
#
# This is the primary guard for feature access.
# Each role has a FeaturePermission row per feature with boolean flags
# (can_view, can_create, can_edit, can_delete), and users may also have
# per-user overrides through User.has_permission().
# Superusers bypass all checks.
# ─────────────────────────────────────────────────────────────────────────────

def permission_required(feature: FEATURES, action: str):
    """
    Factory that returns a FastAPI dependency enforcing a feature permission.

    Parameters
    ----------
    feature : FEATURES
        The feature being accessed, e.g. FEATURES.ARTICLE.
    action : str
        One of "view", "create", "edit", "delete".

    Example:
        @router.post("/articles")
        async def create_article(
            user = Depends(permission_required(FEATURES.ARTICLE, "create"))
        ):
            ...
    """
    async def wrapper(current_user: User = Depends(get_current_user)) -> User:
        allowed = await current_user.has_permission(feature, action)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You do not have '{action}' permission on '{feature}'.",
            )
        return current_user

    return wrapper
