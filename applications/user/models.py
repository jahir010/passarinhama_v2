from enum import Enum
import uuid
from datetime import datetime, timezone, timedelta

from passlib.context import CryptContext
from tortoise import fields, models

pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")


# ─────────────────────────────────────────
# Enums
# ─────────────────────────────────────────

class FEATURES(str, Enum):
    USER       = "user"
    FORUM      = "forum"
    ARTICLE    = "article"
    TRAINING   = "training"
    EVENT      = "event"
    DOCUMENT   = "document"


class UserStatus(str, Enum):
    PENDING   = "pending"
    ACTIVE    = "active"
    SUSPENDED = "suspended"


class ActivityActionType(str, Enum):
    USER_REGISTERED     = "user_registered"
    USER_VALIDATED      = "user_validated"
    ARTICLE_PUBLISHED   = "article_published"
    ARTICLE_UPDATED     = "article_updated"
    POST_CREATED        = "post_created"
    POST_APPROVED       = "post_approved"
    POST_REJECTED       = "post_rejected"
    POST_FLAGGED        = "post_flagged"
    POST_UPDATED        = "post_updated"
    POST_DELETED        = "post_deleted"
    TOPIC_CREATED       = "topic_created"
    TOPIC_UPDATED       = "topic_updated"
    TOPIC_DELETED       = "topic_deleted"
    FORUM_UPDATED       = "forum_updated"
    FORUM_DELETED       = "forum_deleted"
    EVENT_CREATED       = "event_created"
    TRAINING_REGISTERED = "training_registered"
    TRAINING_CREATED    = "training_created"
    DOCUMENT_UPLOADED   = "document_uploaded"
    DOCUMENT_DELETED    = "document_deleted"
    MODERATION_FLAG     = "moderation_flag"
    POST_FORWARDED      = "post_forwarded"
    PROFILE_UPDATED     = "profile_updated"
    PASSWORD_CHANGED    = "password_changed"


# ─────────────────────────────────────────
# Role  (dynamic, created at runtime)
# ─────────────────────────────────────────

class Role(models.Model):
    id          = fields.UUIDField(pk=True, default=uuid.uuid4)
    name        = fields.CharField(max_length=100, unique=True)
    slug        = fields.CharField(max_length=100, unique=True)
    description = fields.TextField(null=True)
    created_at  = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table    = "roles"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# ─────────────────────────────────────────
# FeaturePermission  (one row per role+feature)
# ─────────────────────────────────────────

class FeaturePermission(models.Model):
    id         = fields.UUIDField(pk=True, default=uuid.uuid4)
    role       = fields.ForeignKeyField(
        "models.Role",
        related_name="feature_permissions",
        on_delete=fields.CASCADE,
    )
    feature    = fields.CharEnumField(FEATURES, default=FEATURES.USER)
    can_view   = fields.BooleanField(default=False)
    can_create = fields.BooleanField(default=False)
    can_edit   = fields.BooleanField(default=False)
    can_delete = fields.BooleanField(default=False)

    class Meta:
        table           = "feature_permissions"
        unique_together = [("role", "feature")]

    def __str__(self) -> str:
        return f"{self.role_id} → {self.feature}"


# ─────────────────────────────────────────
# User
# ─────────────────────────────────────────

class UserFeaturePermission(models.Model):
    """
    Stores user-specific permission overrides for a feature.

    Any flag left as NULL falls back to the user's role permission.
    """
    id         = fields.UUIDField(pk=True, default=uuid.uuid4)
    user       = fields.ForeignKeyField(
        "models.User",
        related_name="feature_permission_overrides",
        on_delete=fields.CASCADE,
    )
    feature    = fields.CharEnumField(FEATURES, default=FEATURES.USER)
    can_view   = fields.BooleanField(null=True)
    can_create = fields.BooleanField(null=True)
    can_edit   = fields.BooleanField(null=True)
    can_delete = fields.BooleanField(null=True)

    class Meta:
        table           = "user_feature_permissions"
        unique_together = [("user", "feature")]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.feature}"


class User(models.Model):
    id                   = fields.UUIDField(pk=True, default=uuid.uuid4)
    email                = fields.CharField(max_length=255, unique=True)
    password             = fields.CharField(max_length=255)

    # ── Personal info ─────────────────────────────────────────────────────
    first_name           = fields.CharField(max_length=100)
    last_name            = fields.CharField(max_length=100)
    phone                = fields.CharField(max_length=30, null=True)
    mobile               = fields.CharField(max_length=30, null=True)
    avatar_url           = fields.CharField(max_length=500, null=True)

    # ── Email verification ────────────────────────────────────────────────
    is_email_verified    = fields.BooleanField(default=False)
    email_verified_at    = fields.DatetimeField(null=True)

    # ── Address ───────────────────────────────────────────────────────────
    street_address       = fields.CharField(max_length=255, null=True)
    city                 = fields.CharField(max_length=100, null=True)
    society              = fields.CharField(max_length=255, null=True)
    department           = fields.CharField(max_length=20, null=True)
    country              = fields.CharField(max_length=100, default="France")

    # ── Role & status ─────────────────────────────────────────────────────
    role                 = fields.ForeignKeyField(
        "models.Role",
        related_name="users",
        null=True,
        on_delete=fields.SET_NULL,
    )
    status               = fields.CharEnumField(UserStatus, default=UserStatus.PENDING)
    is_superuser         = fields.BooleanField(default=False)

    # ── Two-factor authentication ──────────────────────────────────────────
    is_active_2fa        = fields.BooleanField(default=False)

    # ── Payment validation ─────────────────────────────────────────────────
    is_payment_validated = fields.BooleanField(default=False)
    validated_by         = fields.ForeignKeyField(
        "models.User",
        related_name="validated_members",
        null=True,
        on_delete=fields.SET_NULL,
    )
    validated_at         = fields.DatetimeField(null=True)

    # ── Soft delete ───────────────────────────────────────────────────────
    is_deleted           = fields.BooleanField(default=False)

    # ── Online presence ───────────────────────────────────────────────────
    last_seen_at         = fields.DatetimeField(null=True)
    last_login_at        = fields.DatetimeField(null=True)

    # ── Timestamps ────────────────────────────────────────────────────────
    member_since         = fields.DatetimeField(null=True)
    created_at           = fields.DatetimeField(auto_now_add=True)
    updated_at           = fields.DatetimeField(auto_now=True)

    class Meta:
        table    = "users"
        ordering = ["last_name", "first_name"]
        indexes  = [
            ("status", "is_payment_validated"),
            ("last_seen_at",),
        ]

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def initials(self) -> str:
        return f"{self.first_name[:1]}{self.last_name[:1]}".upper()

    @property
    def is_active(self) -> bool:
        """A user is active when their status is ACTIVE and they are not soft-deleted."""
        return self.status == UserStatus.ACTIVE and not self.is_deleted

    @property
    def is_online(self) -> bool:
        """True when the user was seen in the last 5 minutes."""
        if not self.last_seen_at:
            return False
        return (datetime.now(timezone.utc) - self.last_seen_at) < timedelta(minutes=5)

    @property
    def membership_year(self) -> int | None:
        if self.validated_at:
            return self.validated_at.year
        if self.member_since:
            return self.member_since.year
        return None

    # ── Permission check ─────────────────────────────────────────────────
    # action: "view" | "create" | "edit" | "delete"
    # feature: FEATURES enum value  e.g. FEATURES.ARTICLE
    #
    # Superusers bypass all permission checks (always True).
    # User-specific overrides are checked before role permissions.
    # Users without a role are denied unless an explicit user override allows it.

    async def has_permission(self, feature: FEATURES, action: str) -> bool:
        if self.is_superuser:
            return True

        action_map = {
            "view":   "can_view",
            "create": "can_create",
            "edit":   "can_edit",
            "delete": "can_delete",
        }
        field_name = action_map.get(action)
        if field_name is None:
            raise ValueError(f"Unknown permission action: '{action}'. Must be one of {list(action_map)}")

        override = await UserFeaturePermission.get_or_none(user_id=self.id, feature=feature)
        if override is not None:
            override_value = getattr(override, field_name, None)
            if override_value is not None:
                return bool(override_value)

        try:
            role_id = self.role_id  # avoid extra DB hit if already fetched
        except Exception:
            return False

        if role_id is None:
            return False

        perm = await FeaturePermission.get_or_none(role_id=role_id, feature=feature)
        if perm is None:
            return False

        return bool(getattr(perm, field_name, False))

    # ── Auth helpers ──────────────────────────────────────────────────────

    @classmethod
    def set_password(cls, password: str) -> str:
        return pwd_context.hash(password)

    def verify_password(self, password: str) -> bool:
        if not self.password:
            return False
        try:
            return pwd_context.verify(password, self.password)
        except Exception:
            return False

    def __str__(self) -> str:
        return self.full_name


# ─────────────────────────────────────────
# UserSession
# ─────────────────────────────────────────

class UserSession(models.Model):
    id           = fields.UUIDField(pk=True, default=uuid.uuid4)
    user         = fields.ForeignKeyField(
        "models.User", related_name="sessions", on_delete=fields.CASCADE
    )
    token_hash   = fields.CharField(max_length=255, unique=True)
    device_name  = fields.CharField(max_length=200, null=True)
    ip_address   = fields.CharField(max_length=45, null=True)
    user_agent   = fields.TextField(null=True)
    is_active    = fields.BooleanField(default=True)
    created_at   = fields.DatetimeField(auto_now_add=True)
    last_used_at = fields.DatetimeField(auto_now_add=True)
    expires_at   = fields.DatetimeField(null=True)

    class Meta:
        table    = "user_sessions"
        ordering = ["-last_used_at"]

    def __str__(self) -> str:
        return f"{self.user_id} — {self.device_name or 'unknown device'}"


# ─────────────────────────────────────────
# ActivityLog
# ─────────────────────────────────────────

class ActivityLog(models.Model):
    id          = fields.UUIDField(pk=True, default=uuid.uuid4)
    user        = fields.ForeignKeyField(
        "models.User", related_name="activity_logs", on_delete=fields.CASCADE
    )
    action_type = fields.CharEnumField(ActivityActionType, max_length=100)
    target_type = fields.CharField(max_length=50, null=True)
    target_id   = fields.UUIDField(null=True)
    description = fields.TextField(null=True)
    created_at  = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table    = "activity_logs"
        ordering = ["-created_at"]
