from enum import Enum
import uuid
from typing import List, Optional

from tortoise import fields, models


class NotificationType(str, Enum):
    NEW_ARTICLE      = "new_article"
    NEW_POST         = "new_post"
    NEW_EVENT        = "new_event"
    NEW_TRAINING     = "new_training"
    POST_REPLY       = "post_reply"
    POST_REJECTED    = "post_rejected"
    ACCOUNT_APPROVED = "account_approved"


# ─────────────────────────────────────────────────────────────
# NotificationPreference
# ─────────────────────────────────────────────────────────────

class NotificationPreference(models.Model):
    """
    Per-user, per-type email opt-in/out.
    One row per (user, notification_type, forum) triple.
    forum=None means the preference applies platform-wide.
    """
    id                = fields.UUIDField(pk=True, default=uuid.uuid4)
    user              = fields.ForeignKeyField(
        "models.User", related_name="notification_preferences", on_delete=fields.CASCADE
    )
    notification_type = fields.CharEnumField(NotificationType, max_length=50)
    email_enabled = fields.BooleanField(default=True)
    updated_at    = fields.DatetimeField(auto_now=True)

    class Meta:
        table           = "notification_preferences"
        unique_together = [("user", "notification_type")]

    # ------------------------------------------------------------------
    # Class-level helpers
    # ------------------------------------------------------------------

    @classmethod
    async def is_enabled(
        cls,
        user_id,
        notification_type: NotificationType,
        forum_id=None,
    ) -> bool:
        """
        Return True when the user has email notifications enabled for
        the given type (and optional forum).  Defaults to True when no
        preference row exists yet.
        """
        pref = await cls.get_or_none(
            user_id=user_id,
            notification_type=notification_type,
            forum_id=forum_id,
        )
        return pref.email_enabled if pref is not None else True

    @classmethod
    async def create_defaults(cls, user_id) -> None:
        """
        Seed all platform-wide notification types as enabled=True for a
        newly registered user.  Call this from the registration flow so
        every user starts with a full set of preference rows and the
        preferences UI is never empty.
        """
        for ntype in NotificationType:
            await cls.get_or_create(
                user_id=user_id,
                notification_type=ntype,
                forum_id=None,
                defaults={"email_enabled": True},
            )

    @classmethod
    async def opted_in_user_ids(
        cls,
        notification_type: NotificationType,
        forum_id=None,
    ) -> List:
        """
        Return the list of user PKs that have NOT opted out of
        `notification_type`.  Users with no preference row are treated
        as opted-in (opt-out model).

        Usage in a bulk-send flow:
            user_ids = await NotificationPreference.opted_in_user_ids(
                NotificationType.NEW_ARTICLE
            )
            users = await User.filter(
                id__in=user_ids,
                status=UserStatus.ACTIVE,
                is_payment_validated=True,
                is_deleted=False,
            ).values_list("email", flat=True)
        """
        opted_out = set(
            await cls.filter(
                notification_type=notification_type,
                forum_id=forum_id,
                email_enabled=False,
            ).values_list("user_id", flat=True)
        )
        from applications.user.models import User  # local import avoids circular deps
        all_ids = set(await User.all().values_list("id", flat=True))
        return list(all_ids - opted_out)


# ─────────────────────────────────────────────────────────────
# NotificationLog
# ─────────────────────────────────────────────────────────────

class NotificationLog(models.Model):
    """Audit log of every notification dispatched by the platform."""
    id                = fields.UUIDField(pk=True, default=uuid.uuid4)
    recipient         = fields.ForeignKeyField(
        "models.User", related_name="notification_logs", on_delete=fields.CASCADE
    )
    notification_type = fields.CharEnumField(NotificationType, max_length=50)
    target_type       = fields.CharField(max_length=50)   # "article" | "post" | "event" | "training"
    target_id         = fields.UUIDField(null=True)
    is_read           = fields.BooleanField(default=False)
    sent_at           = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table    = "notification_logs"
        ordering = ["-sent_at"]

    @classmethod
    async def bulk_create_for_users(
        cls,
        *,
        user_ids: List,
        notification_type: NotificationType,
        target_type: str,
        target_id: Optional[uuid.UUID] = None,
    ) -> None:
        """
        Insert one log row per user in a single DB round-trip.
        Call this after a bulk email send completes.
        """
        await cls.bulk_create([
            cls(
                recipient_id=uid,
                notification_type=notification_type,
                target_type=target_type,
                target_id=target_id,
            )
            for uid in user_ids
        ])




