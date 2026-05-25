from enum import Enum
import uuid

from passlib.context import CryptContext
from tortoise import fields, models






class TrainingFormat(str, Enum):
    ONLINE     = "online"
    IN_PERSON  = "in_person"
    HYBRID     = "hybrid"
 
 
class TrainingStatus(str, Enum):
    OPEN      = "open"
    FULL      = "full"
    COMPLETED = "completed"



# ─────────────────────────────────────────
# 15. Training
# ─────────────────────────────────────────
 
class Training(models.Model):
    id              = fields.UUIDField(pk=True, default=uuid.uuid4)
    title           = fields.CharField(max_length=300)
    description     = fields.TextField(null=True)
    instructor_name = fields.CharField(max_length=100, null=True)
    format          = fields.CharEnumField(TrainingFormat, default=TrainingFormat.ONLINE)
    training_date   = fields.DateField(null=True)
    end_date        = fields.DateField(null=True)
    duration_hours  = fields.IntField(null=True)
    thumbnail_url   = fields.CharField(max_length=500, null=True)  # object storage key
    max_attendees   = fields.IntField(null=True)
    attachments     = fields.JSONField(null=True)  # List of file URLs or metadata
    status          = fields.CharEnumField(TrainingStatus, default=TrainingStatus.OPEN)
    created_by      = fields.ForeignKeyField("models.User", related_name="trainings", on_delete=fields.RESTRICT)
    created_at      = fields.DatetimeField(auto_now_add=True)
    updated_at      = fields.DatetimeField(auto_now=True)
 
    class Meta:
        table    = "trainings"
        ordering = ["training_date"]
 
    def __str__(self) -> str:
        return self.title
    

# ─────────────────────────────────────────
# 16. TrainingRole Permission
# ─────────────────────────────────────────

class TrainingRolePermission(models.Model):
    id       = fields.UUIDField(pk=True, default=uuid.uuid4)
    training    = fields.ForeignKeyField("models.Training", related_name="role_permissions", on_delete=fields.CASCADE)
    role     = fields.ForeignKeyField("models.Role", related_name="training_permissions", on_delete=fields.CASCADE)
    can_read = fields.BooleanField(default=False)
    can_write = fields.BooleanField(default=False)
 
    class Meta:
        table           = "training_role_permissions"
        unique_together = [("training", "role")]
 
 
# ─────────────────────────────────────────
# 16. TrainingRegistration
# ─────────────────────────────────────────
 
class TrainingRegistration(models.Model):
    """Junction: User ↔ Training registration."""
    id            = fields.UUIDField(pk=True, default=uuid.uuid4)
    training      = fields.ForeignKeyField("models.Training", related_name="registrations", on_delete=fields.CASCADE)
    user          = fields.ForeignKeyField("models.User", related_name="training_registrations", on_delete=fields.CASCADE)
    registered_at = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table           = "training_registrations"
        unique_together = [("training", "user")]