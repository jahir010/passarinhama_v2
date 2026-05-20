import uuid
from datetime import UTC, datetime
 
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
 
from applications.notifications.notifications import (
    NotificationLog,
    NotificationPreference,
    NotificationType,
)
from applications.user.models import User, UserStatus
from app.auth import role_required
from app.token import get_current_user
from applications.articles.models import Article, ArticleStatus

 
router = APIRouter()
 
 
# ── Notifications ────────────────────────────────────────────────────────────
 
 
class NotificationPrefUpdate(BaseModel):
    notification_type: NotificationType
    email_enabled: bool
 
 
@router.get("/notifications/preferences", tags=["Notifications"])
async def get_notification_preferences(
    current_user: User = Depends(get_current_user),
):
    """
    Return the current user's notification preferences.
    If no rows exist yet (e.g. legacy accounts), seed defaults first so
    the UI always gets a complete list rather than an empty array.
    """
    prefs = await NotificationPreference.filter(user=current_user)
    if not prefs:
        await NotificationPreference.create_defaults(current_user.id)
        prefs = await NotificationPreference.filter(user=current_user)
    return prefs
 
 
@router.patch("/notifications/preferences", tags=["Notifications"])
async def update_notification_preference(
    body: NotificationPrefUpdate,
    current_user: User = Depends(get_current_user),
):
    pref, _ = await NotificationPreference.get_or_create(
        user=current_user,
        notification_type=body.notification_type,
        defaults={"email_enabled": body.email_enabled},
    )
    pref.email_enabled = body.email_enabled
    await pref.save(update_fields=["email_enabled", "updated_at"])
    return pref


