from enum import Enum
import uuid

from passlib.context import CryptContext
from tortoise import fields, models




class EventType(str, Enum):
    GENERAL    = "general"
    TRAINING   = "training"
    COMMISSION = "commission"



# ─────────────────────────────────────────
# 13. Event
# ─────────────────────────────────────────
 
class Event(models.Model):
    """Calendar event: board meetings, trainings, commissions, general."""
    id            = fields.UUIDField(pk=True, default=uuid.uuid4)
    title         = fields.CharField(max_length=300)
    event_type    = fields.CharEnumField(EventType, default=EventType.GENERAL)
    event_date    = fields.DateField()
    end_date      = fields.DateField(null=True)
    event_time    = fields.TimeField(null=True)
    location      = fields.CharField(max_length=300, null=True)
    description   = fields.TextField(null=True)
    max_attendees = fields.IntField(null=True)
    is_public     = fields.BooleanField(default=False)
    created_by    = fields.ForeignKeyField("models.User", related_name="events", on_delete=fields.RESTRICT)
    created_at    = fields.DatetimeField(auto_now_add=True)
    updated_at    = fields.DatetimeField(auto_now=True)
 
    class Meta:
        table    = "events"
        ordering = ["event_date", "event_time"]
 
    def __str__(self) -> str:
        return self.title
 
 
# ─────────────────────────────────────────
# 14. EventRegistration
# ─────────────────────────────────────────
 
class EventRegistration(models.Model):
    """Junction: User ↔ Event registration."""
    id            = fields.UUIDField(pk=True, default=uuid.uuid4)
    event         = fields.ForeignKeyField("models.Event", related_name="registrations", on_delete=fields.CASCADE)
    user          = fields.ForeignKeyField("models.User", related_name="event_registrations", on_delete=fields.CASCADE)
    registered_at = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table           = "event_registrations"
        unique_together = [("event", "user")]