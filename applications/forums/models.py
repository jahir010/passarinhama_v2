from enum import Enum
import uuid

from passlib.context import CryptContext
from tortoise import fields, models



# ─────────────────────────────────────────
# 9. Forum
# ─────────────────────────────────────────


class ModerationStatus(str, Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FLAGGED  = "flagged"


class ModerationAction(str, Enum):
    APPROVE = "approve"
    REJECT  = "reject"
    FLAG    = "flag"
    FORWARD = "forward"
 
class Forum(models.Model):
    id          = fields.UUIDField(pk=True, default=uuid.uuid4)
    name        = fields.CharField(max_length=200, unique=True)
    slug        = fields.CharField(max_length=200, unique=True)
    author_name = fields.CharField(max_length=100)  # denormalised for display
    description = fields.TextField(null=True)
    forum_type  = fields.CharField(max_length=50, default="general")
    is_active   = fields.BooleanField(default=True)
    created_at  = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table    = "forums"
        ordering = ["name"]
 
    def __str__(self) -> str:
        return self.name
 
 
# ─────────────────────────────────────────
# 10. ForumRolePermission
# ─────────────────────────────────────────
 
class ForumRolePermission(models.Model):
    id       = fields.UUIDField(pk=True, default=uuid.uuid4)
    forum    = fields.ForeignKeyField("models.Forum", related_name="role_permissions", on_delete=fields.CASCADE)
    role     = fields.ForeignKeyField("models.Role", related_name="forum_permissions", on_delete=fields.CASCADE)
    can_read = fields.BooleanField(default=False)
    can_post = fields.BooleanField(default=False)
 
    class Meta:
        table           = "forum_role_permissions"
        unique_together = [("forum", "role")]
 
 
# ─────────────────────────────────────────
# 11. Topic
# ─────────────────────────────────────────
 
class Topic(models.Model):
    """Discussion thread inside a Forum."""
    id               = fields.UUIDField(pk=True, default=uuid.uuid4)
    forum            = fields.ForeignKeyField("models.Forum", related_name="topics", on_delete=fields.CASCADE)
    author           = fields.ForeignKeyField("models.User", related_name="topics", on_delete=fields.RESTRICT)
    title            = fields.CharField(max_length=500)
    content          = fields.TextField(null=True)
    attachment       = fields.JSONField(null=True)  # e.g. {"url": "...", "type": "image/png"}
    is_pinned        = fields.BooleanField(default=False)
    is_locked        = fields.BooleanField(default=False)
    view_count       = fields.IntField(default=0)           # denormalised — updated async
    reply_count      = fields.IntField(default=0)           # denormalised — updated on post approve
    last_activity_at = fields.DatetimeField(auto_now_add=True)
    created_at       = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table    = "topics"
        ordering = ["-last_activity_at"]
 
    def __str__(self) -> str:
        return self.title
 
 
# ─────────────────────────────────────────
# 12. Post
# ─────────────────────────────────────────
 
class Post(models.Model):
    """
    Individual reply within a Topic.
    Enters moderation queue (pending) unless author is admin/moderator.
    """
    id                = fields.UUIDField(pk=True, default=uuid.uuid4)
    topic             = fields.ForeignKeyField("models.Topic", related_name="posts", on_delete=fields.CASCADE)
    author            = fields.ForeignKeyField("models.User", related_name="posts", on_delete=fields.RESTRICT)
    content           = fields.TextField()
    attachment        = fields.JSONField(null=True)  # e.g. {"url": "...", "type": "image/png"}
    moderation_status = fields.CharEnumField(ModerationStatus, default=ModerationStatus.PENDING)
    moderated_by      = fields.ForeignKeyField(
        "models.User",
        related_name="moderated_posts",
        null=True,
        on_delete=fields.SET_NULL,
    )
    assigned_moderator = fields.ForeignKeyField(
        "models.User",
        related_name="assigned_posts",
        null=True,
        on_delete=fields.SET_NULL,
    )  # set when a post is forwarded to another moderator for review
    rejection_reason  = fields.TextField(null=True)
    moderated_at      = fields.DatetimeField(null=True)
    created_at        = fields.DatetimeField(auto_now_add=True)
    updated_at        = fields.DatetimeField(auto_now=True)
 
    class Meta:
        table    = "posts"
        ordering = ["created_at"]



# ─────────────────────────────────────────
# 22. ModerationLog
# ─────────────────────────────────────────
 
class ModerationLog(models.Model):
    """Audit trail of every moderation action. Visible to admin only."""
    id          = fields.UUIDField(pk=True, default=uuid.uuid4)
    moderator   = fields.ForeignKeyField("models.User", related_name="moderation_logs", on_delete=fields.RESTRICT)
    post        = fields.ForeignKeyField("models.Post", related_name="moderation_logs", on_delete=fields.CASCADE)
    action      = fields.CharEnumField(ModerationAction)
    reason      = fields.TextField(null=True)
    acted_at    = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table    = "moderation_logs"
        ordering = ["-acted_at"]