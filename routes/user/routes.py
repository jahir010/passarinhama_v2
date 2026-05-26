from typing import List, Optional
from uuid import UUID
import uuid
import re
from datetime import datetime, timezone as UTC, timedelta
import csv
from io import StringIO
from fastapi.responses import StreamingResponse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, UploadFile, File, Form
from tortoise.expressions import Q
from pydantic import BaseModel, EmailStr, Field

from app.auth import login_required, permission_required, superuser_required
from app.token import get_current_user
from app.utils.send_email import send_email
from applications.user.models import (
    FEATURES, FeaturePermission, UserFeaturePermission, User, Role, UserStatus,
    ActivityActionType, ActivityLog, UserSession,
)
from app.utils.file_manager import update_file, save_file, delete_file


router = APIRouter()

ONLINE_THRESHOLD_MINUTES = 5


# ─────────────────────────────────────────────────────────────────────────────
# Slug helper
# ─────────────────────────────────────────────────────────────────────────────

def slugify(value: str) -> str:
    """Convert a string to a URL-safe slug."""
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[\s_-]+", "-", value)
    return value.strip("-")


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────────────────────────

class RoleCreate(BaseModel):
    name:        str = Field(..., min_length=1, max_length=100)
    description: str | None = None


class RoleOut(BaseModel):
    id:          uuid.UUID
    name:        str
    slug:        str
    description: str | None
    created_at:  datetime

    class Config:
        from_attributes = True


class FeaturePermissionCreate(BaseModel):
    """Body for creating or fully replacing a role's permission on a feature."""
    role_id:    uuid.UUID
    feature:    FEATURES
    can_view:   bool = False
    can_create: bool = False
    can_edit:   bool = False
    can_delete: bool = False


class FeaturePermissionUpdate(BaseModel):
    """Body for patching individual flags on an existing FeaturePermission."""
    can_view:   bool | None = None
    can_create: bool | None = None
    can_edit:   bool | None = None
    can_delete: bool | None = None


class FeaturePermissionOut(BaseModel):
    id:         uuid.UUID
    role_id:    uuid.UUID
    feature:    FEATURES
    can_view:   bool
    can_create: bool
    can_edit:   bool
    can_delete: bool

    class Config:
        from_attributes = True


class UserFeaturePermissionCreate(BaseModel):
    """Body for creating or fully replacing a user's permission overrides on a feature."""
    user_id:     uuid.UUID
    feature:     FEATURES
    can_view:    bool | None = None
    can_create:  bool | None = None
    can_edit:    bool | None = None
    can_delete:  bool | None = None


class UserFeaturePermissionUpdate(BaseModel):
    """Body for patching individual user override flags on an existing override row."""
    can_view:    bool | None = None
    can_create:  bool | None = None
    can_edit:    bool | None = None
    can_delete:  bool | None = None


class UserFeaturePermissionOut(BaseModel):
    id:          uuid.UUID
    user_id:     uuid.UUID
    feature:     FEATURES
    can_view:    bool | None
    can_create:  bool | None
    can_edit:    bool | None
    can_delete:  bool | None

    class Config:
        from_attributes = True


class SessionOut(BaseModel):
    id:           uuid.UUID
    device_name:  str | None
    ip_address:   str | None
    is_active:    bool
    created_at:   datetime
    last_used_at: datetime
    expires_at:   datetime | None

    class Config:
        from_attributes = True


# ─────────────────────────────────────────────────────────────────────────────
# Serializers
# ─────────────────────────────────────────────────────────────────────────────

def _is_online(user: User) -> bool:
    if not user.last_seen_at:
        return False
    return (datetime.now(UTC.utc) - user.last_seen_at) < timedelta(minutes=ONLINE_THRESHOLD_MINUTES)


def _membership_year(user: User) -> int | None:
    if user.validated_at:
        return user.validated_at.year
    if user.member_since:
        return user.member_since.year
    return None


def _years_as_member(user: User) -> int | None:
    ref = user.member_since or user.validated_at
    if not ref:
        return None
    return max(1, (datetime.now(UTC.utc) - ref).days // 365)


def _serialize_role(role: Role) -> dict:
    return {
        "id":          str(role.id),
        "name":        role.name,
        "slug":        role.slug,
        "description": role.description,
        "created_at":  role.created_at,
    }


async def _role_permission_map(role_id: uuid.UUID | None) -> dict[str, dict[str, bool]]:
    if role_id is None:
        return {}

    rows = await FeaturePermission.filter(role_id=role_id).values(
        "feature", "can_view", "can_create", "can_edit", "can_delete"
    )
    return {
        (row["feature"].value if hasattr(row["feature"], "value") else str(row["feature"])): row
        for row in rows
    }


async def _ensure_user_permissions_seeded(
    user: User,
    previous_role_id: uuid.UUID | None = None,
) -> list[dict]:
    role_permission_map = await _role_permission_map(user.role_id)
    existing_rows = await UserFeaturePermission.filter(user_id=user.id)
    existing_map = {
        (row.feature.value if hasattr(row.feature, "value") else str(row.feature)): row
        for row in existing_rows
    }
    previous_role_permission_map = await _role_permission_map(previous_role_id)

    for feature in FEATURES:
        feature_key = feature.value
        role_perm = role_permission_map.get(feature_key)
        if role_perm is None:
            continue

        current_row = existing_map.get(feature_key)
        if current_row is None:
            current_row = await UserFeaturePermission.create(
                user=user,
                feature=feature,
                can_view=role_perm["can_view"],
                can_create=role_perm["can_create"],
                can_edit=role_perm["can_edit"],
                can_delete=role_perm["can_delete"],
            )
            existing_map[feature_key] = current_row
            continue

        previous_role_perm = previous_role_permission_map.get(feature_key)
        if previous_role_perm and (
            current_row.can_view == previous_role_perm["can_view"]
            and current_row.can_create == previous_role_perm["can_create"]
            and current_row.can_edit == previous_role_perm["can_edit"]
            and current_row.can_delete == previous_role_perm["can_delete"]
        ):
            current_row.can_view = role_perm["can_view"]
            current_row.can_create = role_perm["can_create"]
            current_row.can_edit = role_perm["can_edit"]
            current_row.can_delete = role_perm["can_delete"]
            await current_row.save(update_fields=["can_view", "can_create", "can_edit", "can_delete"])

    return await UserFeaturePermission.filter(user_id=user.id).values(
        "feature", "can_view", "can_create", "can_edit", "can_delete"
    )


def _permission_flags_snapshot(
    can_view: bool,
    can_create: bool,
    can_edit: bool,
    can_delete: bool,
) -> dict[str, bool]:
    return {
        "can_view": can_view,
        "can_create": can_create,
        "can_edit": can_edit,
        "can_delete": can_delete,
    }


async def _sync_role_permission_to_users(
    role_id: uuid.UUID,
    feature: FEATURES,
    previous_flags: dict[str, bool],
    new_flags: dict[str, bool],
) -> None:
    users = await User.filter(role_id=role_id).all()
    if not users:
        return

    user_ids = [user.id for user in users]
    existing_rows = await UserFeaturePermission.filter(user_id__in=user_ids, feature=feature)
    existing_map = {row.user_id: row for row in existing_rows}

    for user in users:
        row = existing_map.get(user.id)
        if row is None:
            await UserFeaturePermission.create(
                user=user,
                feature=feature,
                can_view=new_flags["can_view"],
                can_create=new_flags["can_create"],
                can_edit=new_flags["can_edit"],
                can_delete=new_flags["can_delete"],
            )
            continue

        if (
            row.can_view == previous_flags["can_view"]
            and row.can_create == previous_flags["can_create"]
            and row.can_edit == previous_flags["can_edit"]
            and row.can_delete == previous_flags["can_delete"]
        ):
            row.can_view = new_flags["can_view"]
            row.can_create = new_flags["can_create"]
            row.can_edit = new_flags["can_edit"]
            row.can_delete = new_flags["can_delete"]
            await row.save(update_fields=["can_view", "can_create", "can_edit", "can_delete"])


async def _serialize_user(user: User) -> dict:
    role_permissions = await FeaturePermission.filter(role_id=user.role_id).values(
        "feature", "can_view", "can_create", "can_edit", "can_delete"
    )
    user_permissions = await _ensure_user_permissions_seeded(user)

    return {
        "id":                   str(user.id),
        "email":                user.email,
        "first_name":           user.first_name,
        "last_name":            user.last_name,
        "initials":             user.initials,
        "avatar_url":           user.avatar_url,
        "phone":                user.phone,
        "mobile":               user.mobile,
        "address":              user.street_address,
        "city":                 user.city,
        "society":              user.society,
        "department":           user.department,
        #"role_id":              str(user.role_id) if user.role_id else None,
        "role":                 _serialize_role(user.role) if hasattr(user, "role") and user.role else None,
        "status":               user.status,
        "is_superuser":         user.is_superuser,
        "is_payment_validated": user.is_payment_validated,
        "membership_year":      _membership_year(user),
        "is_email_verified":    user.is_email_verified,
        "is_online":            _is_online(user),
        "last_seen_at":         user.last_seen_at,
        "member_since":         user.member_since,
        "last_login_at":        user.last_login_at,
        "created_at":           user.created_at,
        "is_deleted":           user.is_deleted,
        "permissions":          role_permissions,
        "user_permissions":     user_permissions,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Activity helper
# ─────────────────────────────────────────────────────────────────────────────

async def log_activity(
    user: User,
    action: ActivityActionType,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    description: str | None = None,
) -> None:
    await ActivityLog.create(
        user=user,
        action_type=action,
        target_type=target_type,
        target_id=target_id,
        description=description,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Email helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _notify_user_payment_validated(user: User):
    try:
        await send_email(
            subject=f"Membership {'validated' if user.is_payment_validated else 'revoked'}",
            to=user.email,
            html_message=f"""
                <html><body>
                    <p>Hello {user.first_name},</p>
                    <p>Your membership has been
                    {'validated' if user.is_payment_validated else 'revoked'}.</p>
                </body></html>
            """,
        )
    except Exception as e:
        print(f"[notify] Failed to send payment notification: {e}", flush=True)


async def _notify_new_member(user: User, password: str):
    try:
        await send_email(
            subject="Your Account Has Been Created",
            to=user.email,
            html_message=f"""
            <html><body>
            <p>Hi {user.first_name},</p>
            <p>Your account has been created by an administrator.</p>
            <p><strong>Email:</strong> {user.email}<br>
               <strong>Temporary Password:</strong> {password}</p>
            <p>Please change your password after first login.</p>
            <div style="text-align:center; margin:20px 0;">
                <a href="https://example.com/login"
                   style="background:#4CAF50;color:#fff;padding:12px 20px;
                          text-decoration:none;border-radius:5px;">
                    Login to Your Account
                </a>
            </div>
            </body></html>
            """,
        )
    except Exception as e:
        print(f"[notify] Failed to send new member notification: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Roles
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/roles", tags=["Roles & Permissions"])
async def list_roles(current_user: User = Depends(login_required)):
    roles = await Role.all()
    return [_serialize_role(role) for role in roles]

@router.get("/permissions/roles", tags=["Roles & Permissions"])
async def list_roles_with_permissions(feature: FEATURES | None = None, current_user: User = Depends(login_required)):
    qs = Role.all().prefetch_related("feature_permissions")
    if feature:
        qs = qs.filter(feature_permissions__feature=feature, feature_permissions__can_view=True)
    roles = await qs
    
    return [_serialize_role(role) for role in roles]


@router.post("/roles", tags=["Roles & Permissions"], status_code=201)
async def create_role(
    body: RoleCreate,
    current_user: User = Depends(superuser_required),
):
    """Create a new dynamic role (superuser only)."""
    slug = slugify(body.name)
    if await Role.filter(slug=slug).exists():
        raise HTTPException(status_code=409, detail="A role with this name already exists.")
    role = await Role.create(
        name=body.name,
        slug=slug,
        description=body.description,
    )
    for feature in list(FEATURES):
        _, created = await FeaturePermission.get_or_create(
            role=role,
            feature=feature,
            defaults={
                "can_view":   False,
                "can_create": False,
                "can_edit":   False,
                "can_delete": False,
            },
        )
        
    return _serialize_role(role)


@router.patch("/roles/{role_id}", tags=["Roles & Permissions"])
async def update_role(
    role_id: uuid.UUID,
    body: RoleCreate,
    current_user: User = Depends(superuser_required),
):
    """Update a role's name / description (superuser only)."""
    role = await Role.get_or_none(id=role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")

    if body.name:
        role.name = body.name
        role.slug = slugify(body.name)
    if body.description is not None:
        role.description = body.description

    await role.save()
    return _serialize_role(role)


@router.delete("/roles/{role_id}", status_code=204, tags=["Roles & Permissions"])
async def delete_role(
    role_id: uuid.UUID,
    current_user: User = Depends(superuser_required),
):
    """Delete a role (superuser only). Users with this role will have role set to NULL."""
    role = await Role.get_or_none(id=role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")
    await role.delete()


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Feature Permissions
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/permissions", tags=["Roles & Permissions"])
async def list_permissions(
    role_id: uuid.UUID | None = None,
    current_user: User = Depends(login_required),
):
    """
    List all feature permissions, optionally filtered by role.

    Example: GET /permissions?role_id=<uuid>
    """
    qs = FeaturePermission.all().prefetch_related("role")
    if role_id:
        qs = qs.filter(role_id=role_id)
    permissions = await qs
    return [FeaturePermissionOut.model_validate(p) for p in permissions]


@router.post("/permissions", tags=["Roles & Permissions"], status_code=201)
async def create_permission(
    body: FeaturePermissionCreate,
    current_user: User = Depends(superuser_required),
):
    """
    Create or replace a role's feature permission (superuser only).
    """
    role = await Role.get_or_none(id=body.role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")

    existing_perm = await FeaturePermission.get_or_none(role=role, feature=body.feature)
    previous_flags = _permission_flags_snapshot(
        can_view=existing_perm.can_view if existing_perm else False,
        can_create=existing_perm.can_create if existing_perm else False,
        can_edit=existing_perm.can_edit if existing_perm else False,
        can_delete=existing_perm.can_delete if existing_perm else False,
    )

    perm, _ = await FeaturePermission.get_or_create(
        role=role,
        feature=body.feature,
        defaults={
            "can_view":   body.can_view,
            "can_create": body.can_create,
            "can_edit":   body.can_edit,
            "can_delete": body.can_delete,
        },
    )
    # If it already existed, update the flags
    perm.can_view   = body.can_view
    perm.can_create = body.can_create
    perm.can_edit   = body.can_edit
    perm.can_delete = body.can_delete
    await perm.save()
    await _sync_role_permission_to_users(
        role_id=role.id,
        feature=body.feature,
        previous_flags=previous_flags,
        new_flags=_permission_flags_snapshot(
            can_view=perm.can_view,
            can_create=perm.can_create,
            can_edit=perm.can_edit,
            can_delete=perm.can_delete,
        ),
    )

    return FeaturePermissionOut.model_validate(perm)


@router.patch("/permissions/{permission_id}", tags=["Roles & Permissions"])
async def update_permission(
    permission_id: uuid.UUID,
    body: FeaturePermissionUpdate,
    current_user: User = Depends(superuser_required),
):
    """Partially update individual permission flags (superuser only)."""
    perm = await FeaturePermission.get_or_none(id=permission_id)
    if not perm:
        raise HTTPException(status_code=404, detail="Permission not found.")

    previous_flags = _permission_flags_snapshot(
        can_view=perm.can_view,
        can_create=perm.can_create,
        can_edit=perm.can_edit,
        can_delete=perm.can_delete,
    )

    if body.can_view is not None:
        perm.can_view = body.can_view
    if body.can_create is not None:
        perm.can_create = body.can_create
    if body.can_edit is not None:
        perm.can_edit = body.can_edit
    if body.can_delete is not None:
        perm.can_delete = body.can_delete

    await perm.save()
    await _sync_role_permission_to_users(
        role_id=perm.role_id,
        feature=perm.feature,
        previous_flags=previous_flags,
        new_flags=_permission_flags_snapshot(
            can_view=perm.can_view,
            can_create=perm.can_create,
            can_edit=perm.can_edit,
            can_delete=perm.can_delete,
        ),
    )
    return FeaturePermissionOut.model_validate(perm)


@router.delete("/permissions/{permission_id}", status_code=204, tags=["Roles & Permissions"])
async def delete_permission(
    permission_id: uuid.UUID,
    current_user: User = Depends(superuser_required),
):
    """Remove a feature permission row from a role (superuser only)."""
    perm = await FeaturePermission.get_or_none(id=permission_id)
    if not perm:
        raise HTTPException(status_code=404, detail="Permission not found.")
    previous_flags = _permission_flags_snapshot(
        can_view=perm.can_view,
        can_create=perm.can_create,
        can_edit=perm.can_edit,
        can_delete=perm.can_delete,
    )
    role_id = perm.role_id
    feature = perm.feature
    await perm.delete()
    await _sync_role_permission_to_users(
        role_id=role_id,
        feature=feature,
        previous_flags=previous_flags,
        new_flags=_permission_flags_snapshot(
            can_view=False,
            can_create=False,
            can_edit=False,
            can_delete=False,
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Member Directory
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/user-permissions", tags=["Roles & Permissions"])
async def list_user_permissions(
    user_id: uuid.UUID | None = None,
    current_user: User = Depends(login_required),
):
    """List all user-specific feature permission overrides, optionally filtered by user."""
    if user_id:
        user = await User.get_or_none(id=user_id, is_deleted=False)
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        await _ensure_user_permissions_seeded(user)

    qs = UserFeaturePermission.all().prefetch_related("user")
    if user_id:
        qs = qs.filter(user_id=user_id)
    permissions = await qs
    return [UserFeaturePermissionOut.model_validate(p) for p in permissions]


@router.post("/user-permissions", tags=["Roles & Permissions"], status_code=201)
async def create_user_permission(
    body: UserFeaturePermissionCreate,
    current_user: User = Depends(superuser_required),
):
    """
    Create or replace a user's feature permission overrides.

    Any field left as null falls back to the user's role permission.
    """
    user = await User.get_or_none(id=body.user_id, is_deleted=False)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    perm, _ = await UserFeaturePermission.get_or_create(
        user=user,
        feature=body.feature,
        defaults={
            "can_view": body.can_view,
            "can_create": body.can_create,
            "can_edit": body.can_edit,
            "can_delete": body.can_delete,
        },
    )
    perm.can_view = body.can_view
    perm.can_create = body.can_create
    perm.can_edit = body.can_edit
    perm.can_delete = body.can_delete
    await perm.save()

    return UserFeaturePermissionOut.model_validate(perm)


@router.patch("/user-permissions/{permission_id}", tags=["Roles & Permissions"])
async def update_user_permission(
    permission_id: uuid.UUID,
    body: UserFeaturePermissionUpdate,
    current_user: User = Depends(superuser_required),
):
    """Partially update individual user override flags."""
    perm = await UserFeaturePermission.get_or_none(id=permission_id)
    if not perm:
        raise HTTPException(status_code=404, detail="User permission override not found.")

    if "can_view" in body.model_fields_set:
        perm.can_view = body.can_view
    if "can_create" in body.model_fields_set:
        perm.can_create = body.can_create
    if "can_edit" in body.model_fields_set:
        perm.can_edit = body.can_edit
    if "can_delete" in body.model_fields_set:
        perm.can_delete = body.can_delete

    await perm.save()
    return UserFeaturePermissionOut.model_validate(perm)


@router.delete("/user-permissions/{permission_id}", status_code=204, tags=["Roles & Permissions"])
async def delete_user_permission(
    permission_id: uuid.UUID,
    current_user: User = Depends(superuser_required),
):
    """Remove a user-specific feature permission override row."""
    perm = await UserFeaturePermission.get_or_none(id=permission_id)
    if not perm:
        raise HTTPException(status_code=404, detail="User permission override not found.")
    await perm.delete()


@router.get("/users", tags=["Members"])
async def list_users(
    search:     str | None = None,
    role_id:    uuid.UUID | None = None,
    status:     UserStatus | None = None,
    alpha:      str | None = Query(None, max_length=1),
    year:       int | None = None,
    archived:   bool = False,
    page:       int = Query(1, ge=1),
    page_size:  int = Query(20, ge=1, le=100),
    current_user: User = Depends(
        permission_required(FEATURES.USER, "view")
    ),
):
    if archived or status == UserStatus.SUSPENDED:
        qs = User.filter(is_deleted=True)
    else:
        qs = User.filter(is_deleted=False)
        if not status:
            qs = qs.filter(status=UserStatus.ACTIVE.value)

    if search:
        qs = qs.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)  |
            Q(email__icontains=search)      |
            Q(city__icontains=search)
        )
    if role_id:
        qs = qs.filter(role_id=role_id)
    if status:
        qs = qs.filter(status=status)
    if alpha:
        qs = qs.filter(last_name__istartswith=alpha)
    if year:
        qs = qs.filter(
            Q(validated_at__year=year) | Q(member_since__year=year)
        )

    total = await qs.count()
    users = await qs.offset((page - 1) * page_size).limit(page_size).prefetch_related("role")

    counts = {
        "all":       await User.filter(is_deleted=False).count(),
        "active":    await User.filter(is_deleted=False, status=UserStatus.ACTIVE).count(),
        "pending":   await User.filter(is_deleted=False, status=UserStatus.PENDING).count(),
        "suspended": await User.filter(is_deleted=True, status=UserStatus.SUSPENDED).count(),
        "archived":  await User.filter(is_deleted=True).count(),
    }

    return {
        "total":   total,
        "page":    page,
        "counts":  counts,
        "results": [await _serialize_user(u) for u in users],
    }


@router.post("/users", tags=["Members"], status_code=201)
async def create_user(
    background_tasks: BackgroundTasks,
    first_name:        str       = Form(...),
    last_name:         str       = Form(...),
    email:             EmailStr  = Form(...),
    password:          str       = Form(...),
    phone:             str | None = Form(None),
    mobile:            str | None = Form(None),
    avatar:            UploadFile | None = File(None),
    address:           str | None = Form(None),
    city:              str | None = Form(None),
    department:        str | None = Form(None),
    society:           str | None = Form(None),
    role_id:           uuid.UUID | None = Form(None),
    status:            UserStatus = Form(UserStatus.PENDING),
    payment_validated: bool = Form(False),
    is_superuser:      bool = Form(False),
    current_user: User = Depends(permission_required(FEATURES.USER, "create")),
):
    """Create a new member account."""
    if await User.filter(email=email).exists():
        raise HTTPException(status_code=409, detail="Email already registered.")

    avatar_url = None
    if avatar and avatar.filename:
        try:
            avatar_url = await save_file(avatar, upload_to="avatars")
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to upload avatar.")

    role = None
    if role_id:
        role = await Role.get_or_none(id=role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found.")

    user = await User.create(
        email=email,
        password=User.set_password(password),
        first_name=first_name,
        last_name=last_name,
        avatar_url=avatar_url,
        street_address=address,
        city=city,
        society=society,
        department=department,
        phone=phone,
        mobile=mobile,
        role=role,
        status=status,
        is_payment_validated=payment_validated,
        member_since=datetime.now(UTC.utc),
        is_superuser=is_superuser,
    )
    await _ensure_user_permissions_seeded(user)
    await log_activity(
        current_user, ActivityActionType.USER_REGISTERED, "user", user.id,
        f"New member created: {user.full_name}",
    )
    background_tasks.add_task(_notify_new_member, user, password)
    return await _serialize_user(user)


@router.get("/users/online", tags=["Members"])
async def list_online_users(
    current_user: User = Depends(permission_required(FEATURES.USER, "view")),
):
    """Users seen in the last 5 minutes."""
    threshold = datetime.now(UTC.utc) - timedelta(minutes=ONLINE_THRESHOLD_MINUTES)
    users = (
        await User.filter(is_deleted=False, last_seen_at__gte=threshold)
        .order_by("-last_seen_at")
        .limit(50)
    )
    return {
        "count": len(users),
        "users": [
            {
                "id":           str(u.id),
                "first_name":   u.first_name,
                "last_name":    u.last_name,
                "initials":     u.initials,
                "avatar_url":   u.avatar_url,
                "last_seen_at": u.last_seen_at,
            }
            for u in users
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Own Profile
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users/me", tags=["Members"])
async def get_me(current_user: User = Depends(login_required)):
    await current_user.fetch_related("role")
    return await _serialize_user(current_user)


@router.patch("/users/me", tags=["Members"])
async def update_me(
    first_name: str | None = Form(None),
    last_name:  str | None = Form(None),
    email:      EmailStr | None = Form(None),
    phone:      str | None = Form(None),
    mobile:     str | None = Form(None),
    avatar:     UploadFile | None = File(None),
    address:    str | None = Form(None),
    city:       str | None = Form(None),
    department: str | None = Form(None),
    society:    str | None = Form(None),
    current_user: User = Depends(login_required),
):
    """Update own profile."""
    if avatar and avatar.filename:
        try:
            current_user.avatar_url = await update_file(avatar, current_user.avatar_url, upload_to="avatars")
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to upload photo.")

    if first_name is not None:
        current_user.first_name = first_name
    if last_name is not None:
        current_user.last_name = last_name
    if email is not None:
        current_user.email = email
    if phone is not None:
        current_user.phone = phone
    if mobile is not None:
        current_user.mobile = mobile
    if address is not None:
        current_user.street_address = address
    if city is not None:
        current_user.city = city
    if department is not None:
        current_user.department = department
    if society is not None:
        current_user.society = society

    await current_user.save()
    await log_activity(current_user, ActivityActionType.PROFILE_UPDATED, "user", current_user.id)
    return await _serialize_user(current_user)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Sessions
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users/me/sessions", tags=["Members"])
async def list_my_sessions(current_user: User = Depends(login_required)):
    sessions = await UserSession.filter(user=current_user, is_active=True).all()
    return [SessionOut.model_validate(s) for s in sessions]


@router.delete("/users/me/sessions/{session_id}", status_code=204, tags=["Members"])
async def revoke_session(
    session_id: uuid.UUID,
    current_user: User = Depends(login_required),
):
    session = await UserSession.get_or_none(id=session_id, user=current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    session.is_active = False
    await session.save(update_fields=["is_active"])


@router.delete("/users/me/sessions", status_code=204, tags=["Members"])
async def revoke_all_sessions(current_user: User = Depends(login_required)):
    await UserSession.filter(user=current_user, is_active=True).update(is_active=False)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Single Member
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users/{user_id}", tags=["Members"])
async def get_user(
    user_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.USER, "view")),
):
    user = await User.get_or_none(id=user_id, is_deleted=False)
    if not user:
        raise HTTPException(status_code=404, detail="Member not found.")
    await user.fetch_related("role")
    return await _serialize_user(user)


@router.patch("/users/{user_id}", tags=["Members"])
async def update_user(
    user_id:    uuid.UUID,
    first_name: str | None = Form(None),
    last_name:  str | None = Form(None),
    phone:      str | None = Form(None),
    mobile:     str | None = Form(None),
    address:    str | None = Form(None),
    city:       str | None = Form(None),
    department: str | None = Form(None),
    society:    str | None = Form(None),
    avatar:     UploadFile | None = File(None),
    role_id:    uuid.UUID | None = Form(None),
    status:     UserStatus | None = Form(None),
    payment_validated: bool | None = Form(None),
    current_user: User = Depends(permission_required(FEATURES.USER, "edit")),
):
    user = await User.get_or_none(id=user_id, is_deleted=False)
    if not user:
        raise HTTPException(status_code=404, detail="Member not found.")
    previous_role_id = user.role_id

    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if phone is not None:
        user.phone = phone
    if mobile is not None:
        user.mobile = mobile
    if address is not None:
        user.street_address = address
    if city is not None:
        user.city = city
    if department is not None:
        user.department = department
    if society is not None:
        user.society = society
    if avatar and avatar.filename:
        try:
            user.avatar_url = await update_file(avatar, user.avatar_url, upload_to="avatars")
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to upload photo.")
    if role_id is not None:
        role = await Role.get_or_none(id=role_id)
        if not role:
            raise HTTPException(status_code=404, detail="Role not found.")
        user.role = role
    if status is not None:
        user.status = status
    if payment_validated is not None:
        user.is_payment_validated = payment_validated

    await user.save()
    if role_id is not None:
        await _ensure_user_permissions_seeded(user, previous_role_id=previous_role_id)
    await user.fetch_related("role")
    return await _serialize_user(user)


@router.patch("/users/{user_id}/photo_upload", tags=["Members"])
async def update_user_photo(
    user_id: uuid.UUID,
    photo: UploadFile = File(...),
    current_user: User = Depends(permission_required(FEATURES.USER, "edit")),
):
    user = await User.get_or_none(id=user_id, is_deleted=False).prefetch_related("role")
    if not user:
        raise HTTPException(status_code=404, detail="Member not found.")
    if photo.filename:
        try:
            user.avatar_url = await update_file(photo, user.avatar_url, upload_to="avatars")
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to upload photo.")
    await user.save()
    return await _serialize_user(user)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Payment Validation
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/users/{user_id}/validate", tags=["Members"])
async def validate_payment(
    user_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(permission_required(FEATURES.USER, "edit")),
):
    """Toggle payment validation."""
    user = await User.get_or_none(id=user_id, is_deleted=False).prefetch_related("role")
    if not user:
        raise HTTPException(status_code=404, detail="Member not found.")

    user.is_payment_validated = not user.is_payment_validated
    if user.is_payment_validated:
        user.status          = UserStatus.ACTIVE
        user.validated_by_id = current_user.id
        user.validated_at    = datetime.now(UTC.utc)
        if not user.member_since:
            user.member_since = datetime.now(UTC.utc)

    await user.save()
    background_tasks.add_task(_notify_user_payment_validated, user)
    await log_activity(
        current_user, ActivityActionType.USER_VALIDATED, "user", user.id,
        f"Payment {'validated' if user.is_payment_validated else 'revoked'} for {user.full_name}",
    )
    return await _serialize_user(user)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Soft Delete / Restore
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/users/{user_id}", status_code=204, tags=["Members"])
async def delete_user(
    user_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.USER, "delete")),
):
    user = await User.get_or_none(id=user_id, is_deleted=False)
    if not user:
        raise HTTPException(status_code=404, detail="Member not found.")

    user.is_deleted = True
    user.status     = UserStatus.SUSPENDED
    await UserSession.filter(user=user, is_active=True).update(is_active=False)
    await user.save(update_fields=["is_deleted", "status"])


@router.patch("/users/{user_id}/restore", tags=["Members"])
async def restore_user(
    user_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.USER, "edit")),
):
    user = await User.get_or_none(id=user_id, is_deleted=True)
    if not user:
        raise HTTPException(status_code=404, detail="Archived member not found.")

    user.is_deleted = False
    user.status     = UserStatus.PENDING
    await user.save(update_fields=["is_deleted", "status"])
    return await _serialize_user(user)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — Dashboard / Stats
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users/export/to_csv", tags=["Dashboard"])
async def export_csv(
    current_user: User = Depends(permission_required(FEATURES.USER, "view")),
):
    users = await User.filter(status__in=[UserStatus.PENDING, UserStatus.ACTIVE]).prefetch_related("role")

    fieldnames = [
        "id", "email", "first_name", "last_name",
        "phone", "mobile", "address", "city",
        "society", "department", "role", "status",
        "member_since", "last_login_at", "created_at",
    ]
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for user in users:
        writer.writerow({
            "id":           str(user.id),
            "email":        user.email,
            "first_name":   user.first_name,
            "last_name":    user.last_name,
            "phone":        user.phone,
            "mobile":       user.mobile,
            "address":      user.street_address,
            "city":         user.city,
            "society":      user.society,
            "department":   user.department,
            "role":         user.role.name if user.role_id else "",
            "status":       user.status.value if hasattr(user.status, "value") else user.status,
            "member_since": user.member_since.isoformat() if user.member_since else "",
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else "",
            "created_at":   user.created_at.isoformat() if user.created_at else "",
        })

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users_export.csv"},
    )


@router.get("/users/stats/roles", tags=["Dashboard"])
async def role_distribution(
    current_user: User = Depends(permission_required(FEATURES.USER, "view")),
):
    from tortoise.functions import Count
    rows = (
        await User.filter(is_deleted=False, status=UserStatus.ACTIVE)
        .prefetch_related("role")
        .group_by("role_id")
        .annotate(count=Count("id"))
        .values("role__name", "count")
    )
    return {row["role__name"]: row["count"] for row in rows}


@router.get("/users/stats/online", tags=["Dashboard"])
async def online_count(
    current_user: User = Depends(permission_required(FEATURES.USER, "view")),
):
    threshold = datetime.now(UTC.utc) - timedelta(minutes=ONLINE_THRESHOLD_MINUTES)
    count = await User.filter(is_deleted=False, last_seen_at__gte=threshold).count()
    return {"online": count}
