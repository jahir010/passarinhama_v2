import ast
from typing import List, Optional
import json

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Form, UploadFile, File
from tortoise.expressions import F
from pydantic import BaseModel, field_validator
import uuid
from datetime import datetime, date, time, timezone as UTC

from app.auth import login_required, role_required, superuser_required, permission_required
from app.token import get_current_user
from app.utils.file_manager import delete_file, save_file
from app.utils.helper_functions import log_activity

from applications.events.models import Event, EventRegistration, EventType
from applications.user.models import User, UserStatus, Role, ActivityActionType, FEATURES
from applications.notifications.notifications import NotificationLog, NotificationType, NotificationPreference
from app.utils.send_email import send_bulk_email, send_email


router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class EventCreate(BaseModel):
    title:         str
    event_type:    EventType       = EventType.GENERAL
    event_date:    str                                  # "YYYY-MM-DD"
    event_time:    str     = None               # "HH:MM"
    end_date:      str | None      = None               # "YYYY-MM-DD"
    location:      str | None      = None
    description:   str | None      = None
    max_attendees: int | None      = None
    is_public:     bool            = False

    # FIX: validate and coerce strings → proper Python types at schema level
    # so the ORM always receives the correct type, not a raw string.
    @field_validator("event_date")
    @classmethod
    def parse_event_date(cls, v: str) -> date:
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError("event_date must be in YYYY-MM-DD format.")
    
    @field_validator("end_date")
    @classmethod
    def parse_end_date(cls, v: str | None) -> date | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError("end_date must be in YYYY-MM-DD format.")


    @field_validator("event_time")
    @classmethod
    def parse_event_time(cls, v: str | None) -> time | None:
        if v is None:
            return None
        try:
            return datetime.strptime(v, "%H:%M").time()
        except ValueError:
            raise ValueError("event_time must be in HH:MM format (24-hour).")


class EventUpdate(BaseModel):
    """All fields optional for PATCH semantics."""
    title:         str | None      = None
    event_type:    EventType | None = None
    event_date:    str | None      = None
    end_date:      str | None      = None
    event_time:    str | None      = None
    location:      str | None      = None
    description:   str | None      = None
    max_attendees: int | None      = None
    is_public:     bool | None     = None

    @field_validator("event_date")
    @classmethod
    def parse_event_date(cls, v: str | None) -> date | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError("event_date must be in YYYY-MM-DD format.")
    
    @field_validator("end_date")
    @classmethod
    def parse_end_date(cls, v: str | None) -> date | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError("end_date must be in YYYY-MM-DD format.")


    @field_validator("event_time")
    @classmethod
    def parse_event_time(cls, v: str | None) -> time | None:
        if v is None:
            return None
        try:
            return datetime.strptime(v, "%H:%M").time()
        except ValueError:
            raise ValueError("event_time must be in HH:MM format (24-hour).")


# ──────────────────────────────────────────────────────────────────────────────
# Shared serialiser
# ──────────────────────────────────────────────────────────────────────────────

async def _notify_new_event(event_id: uuid.UUID, event_title: str) -> None:
    """
    Background task: send NEW_EVENT emails only to users who:
      - are active, payment-validated, and not deleted
      - have NOT opted out of NEW_EVENT notifications
 
    Sends in chunks of 50 to stay within SMTP rate limits, then writes
    a single-batch audit log so the NotificationLog table stays accurate.
    """
    # 1. Resolve opted-in user IDs (excludes explicit opt-outs)
    opted_in_ids = await NotificationPreference.opted_in_user_ids(
        NotificationType.NEW_EVENT
    )
    if not opted_in_ids:
        return
 
    # 2. Filter to only eligible users and fetch their emails
    users = await User.filter(
        id__in=opted_in_ids,
        status=UserStatus.ACTIVE,
        is_payment_validated=True,
        is_deleted=False,
    ).values("id", "email", "first_name")
 
    if not users:
        return
 
    emails     = [u["email"] for u in users]
    user_ids   = [u["id"]    for u in users]
 
    # 3. Build a clean HTML email — never interpolate raw model objects
    html_body = f"""
    <html>
      <body style="font-family: sans-serif; color: #333;">
        <h2>New Event Created</h2>
        <p>A new event is now available on the platform:</p>
        <p><strong>{event_title}</strong></p>
        <p>
          <a href="https://yourplatform.com/events/{event_id}"
             style="background:#4F46E5;color:#fff;padding:10px 20px;
                    border-radius:6px;text-decoration:none;">
            View Event
          </a>
        </p>
        <hr/>
        <small>
          You're receiving this because you subscribed to event notifications.
          <a href="https://yourplatform.com/settings/notifications">Unsubscribe</a>
        </small>
      </body>
    </html>
    """
 
    # 4. Send in chunks — respects SMTP rate limits
    result = await send_bulk_email(
        subject=f"New Event: {event_title}",
        recipients=emails,
        html_message=html_body,
        chunk_size=50,
        chunk_delay=1.0,
        retries=1,
    )
 
    # 5. Write one log row per recipient in a single DB round-trip
    if result["sent"] > 0:
        await NotificationLog.bulk_create_for_users(
            user_ids=user_ids,
            notification_type=NotificationType.NEW_EVENT,
            target_type="event",
            target_id=event_id,
        )
 
    print(
        f"[notify] event={event_id} sent={result['sent']} failed={result['failed']}",
        flush=True,
    )

def _format_event_time(t) -> str | None:
    """
    Tortoise ORM + PostgreSQL returns TimeField values as timedelta, not time.
    This helper handles both types safely and always returns "HH:MM" or None.
    """
    if t is None:
        return None
    from datetime import timedelta
    if isinstance(t, timedelta):
        total_seconds = int(t.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f"{hours:02d}:{minutes:02d}"
    # Fallback: actual time object (SQLite / already coerced)
    return t.strftime("%H:%M")


async def _serialize_event(event: Event, current_user: User | None = None) -> dict:
    """
    Build the dict every UI card/row/detail needs:
      - full fields
      - attendee_count  → capacity bar on cards
      - is_registered   → toggle Register/Unregister button
      - spots_left      → None if no cap, int otherwise
      - created_by name → display in detail view
    """
    attendee_count = await EventRegistration.filter(event=event).count()

    is_registered = False
    if current_user:
        is_registered = await EventRegistration.filter(
            event=event, user=current_user
        ).exists()

    spots_left = None
    if event.max_attendees is not None:
        spots_left = max(0, event.max_attendees - attendee_count)

    created_by = await event.created_by
    return {
        "id":             str(event.id),
        "title":          event.title,
        "event_type":     event.event_type,
        "event_date":     event.event_date.isoformat(),
        "end_date":       event.end_date.isoformat() if event.end_date else None,
        # day/month split — needed by dashboard widget (§5.3)
        "day":            event.event_date.day,
        "month":          event.event_date.strftime("%b"),   # "May", "Jun" …
        "event_time":     _format_event_time(event.event_time),
        "location":       event.location,
        "description":    event.description,
        "max_attendees":  event.max_attendees,
        "attendee_count": attendee_count,
        "spots_left":     spots_left,
        "is_at_capacity": spots_left == 0 if spots_left is not None else False,
        "is_registered":  is_registered,
        "is_public":      event.is_public,
        "attachments":    event.attachments,
        "created_at":     event.created_at.isoformat(),
        "created_by": {
            "id":         str(created_by.id),
            "first_name": created_by.first_name,
            "last_name":  created_by.last_name,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# EVENTS
# IMPORTANT: fixed-path routes (/upcoming, /calendar-stats) MUST be declared
# before parameterised routes (/{event_id}) — otherwise FastAPI matches
# "upcoming" as a UUID string and returns 422.
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/events/upcoming", tags=["Events"])
async def upcoming_events(
    limit:        int  = Query(6, ge=1, le=20),
    current_user: User = Depends(permission_required(FEATURES.EVENT, "view")),        # FIX: requires login per spec §5
):
    """
    Next N upcoming events for the dashboard widget (§5.3).
    Returns day, month, title, location, event_type — exactly what the widget needs.
    Default limit=6 per spec §5.3.
    Spec ref: §5.3
    """
    today  = date.today()
    events = (
        await Event.filter(event_date__gte=today)
        .order_by("event_date", "event_time")
        .limit(limit)
    )
    return [await _serialize_event(e, current_user) for e in events]


@router.get("/events/calendar-stats", tags=["Events"])
async def calendar_stats(
    current_user: User = Depends(permission_required(FEATURES.EVENT, "view")),        
):
    """
    Stats for the dashboard stat card: total events scheduled this year.
    Spec ref: §5.1 ('Events scheduled this year')
    """
    today      = date.today()
    year_start = date(today.year, 1, 1)
    year_end   = date(today.year + 1, 1, 1)
    count      = await Event.filter(
        event_date__gte=year_start, event_date__lt=year_end
    ).count()
    return {"events_this_year": count}


@router.get("/events", tags=["Events"])
async def list_events(
    year:         int | None       = None,
    month:        int | None       = None,
    event_type:   EventType | None = None,
    current_user: User | None      = Depends(permission_required(FEATURES.EVENT, "view")),
):
    """
    Full event list with optional year/month/type filters.
    Used by the monthly calendar grid (§9.2) and the list view.

    - Unauthenticated → only is_public=True events (homepage preview)
    - Authenticated   → all events
    - Filters: ?year=2026&month=5&event_type=training

    Response includes attendee_count and is_registered per event
    so the UI can render capacity bars and Register buttons without
    extra requests.
    Spec ref: §9.1, §9.2
    """
    qs = Event.filter()

    if not current_user:
        qs = qs.filter(is_public=True)

    if year and month:
        start = date(year, month, 1)
        end   = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        qs    = qs.filter(event_date__gte=start, event_date__lt=end)
    elif year:
        qs = qs.filter(event_date__gte=date(year, 1, 1), event_date__lt=date(year + 1, 1, 1))
    elif month:
        raise HTTPException(
            status_code=400,
            detail="month filter requires year to also be specified.",
        )

    if event_type:
        qs = qs.filter(event_type=event_type)

    events = await qs.order_by("event_date", "event_time")
    return [await _serialize_event(e, current_user) for e in events]


@router.get("/events/{event_id}", tags=["Events"])
async def get_event(
    event_id:     uuid.UUID,
    current_user: User | None = Depends(permission_required(FEATURES.EVENT, "view")),
):
    
    event = await Event.get_or_none(id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")
    # Non-public events require login
    if not event.is_public and not current_user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return await _serialize_event(event, current_user)


@router.post("/events", tags=["Events"], status_code=201)
async def create_event(
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    event_type: EventType = Form(EventType.GENERAL),
    event_date: str = Form(...),   # "YYYY-MM-DD"
    event_time: str = Form(None), # "HH:MM"
    end_date: str = Form(None),   # "YYYY-MM-DD"
    location: str = Form(None),
    description: str = Form(None),
    max_attendees: int = Form(None),
    is_public: bool = Form(False),
    attachments:      List[UploadFile]  = File(default=[]),    
    current_user:     User = Depends(permission_required(FEATURES.EVENT, "create")),
):
    parsed_date: Optional[date] = None
    parsed_end_date: Optional[date] = None
    if event_date:
        try:
            parsed_date = date.fromisoformat(event_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="training_date must be YYYY-MM-DD format.")

    if end_date:
        try:
            parsed_end_date = date.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="end_date must be YYYY-MM-DD format.")
    attachment_urls: List[str] = []
    for file in attachments:
        if file.filename:
            file_url = await save_file(file, upload_to="training_attachments")
            attachment_urls.append(file_url)
    event = await Event.create(
        title=title,
        event_type=event_type,
        event_date=parsed_date,
        event_time=event_time,
        end_date=parsed_end_date,
        location=location,
        description=description,
        max_attendees=max_attendees,
        is_public=is_public,
        attachments=attachment_urls,
        created_by=current_user
    )
    await log_activity(current_user, ActivityActionType.EVENT_CREATED, "event", event.id, event.title)

    # FIX: filter by notification preference, not all active users
    # NotificationPreference stores per-user per-type opt-in/out
    prefs = await NotificationPreference.filter(
        notification_type=NotificationType.NEW_EVENT,
        email_enabled=True,
    ).prefetch_related("user")

    try:
        background_tasks.add_task(_notify_new_event, event.id, event.title)
    except Exception as e:
        print(f"[notify] Failed to enqueue notification task: {e}", flush=True)

    return await _serialize_event(event, current_user)


@router.patch("/events/{event_id}", tags=["Events"])
async def update_event(
    event_id:     uuid.UUID,
    title: Optional[str] = Form(None),
    event_type: Optional[EventType] = Form(None),
    event_date: Optional[str] = Form(None),   # "YYYY-MM-DD"
    event_time: Optional[str] = Form(None), # "HH:MM"
    end_date: Optional[str] = Form(None),   # "YYYY-MM-DD"
    location: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    max_attendees: Optional[int] = Form(None),
    is_public: Optional[bool] = Form(None),
    new_attachments: Optional[List[UploadFile]] = File(default=[]),
    remove_attachment_urls: Optional[str]       = Form(None),
    current_user: User = Depends(permission_required(FEATURES.EVENT, "edit")),
):
    event = await Event.get_or_none(id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    if title is not None:
        event.title = title
    if event_type is not None:
        event.event_type = event_type

    if event_date is not None:
        try:
            event.event_date = date.fromisoformat(event_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="event_date must be YYYY-MM-DD format.")
    if end_date is not None:
        try:
            event.end_date = date.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="end_date must be YYYY-MM-DD format.")
    if event_time is not None:
        try:
            event.event_time = datetime.strptime(event_time, "%H:%M").time()
        except ValueError:
            raise HTTPException(status_code=422, detail="event_time must be HH:MM format.")
    if location is not None:
        event.location = location
    if description is not None:
        event.description = description
    if max_attendees is not None:
         event.max_attendees = max_attendees
    if is_public is not None:
        event.is_public = is_public
    
    current_attachments: List[str] = event.attachments or []
    if remove_attachment_urls:
        raw = remove_attachment_urls.strip()

        urls_to_remove: List[str] = []

        try:
            # Case 1: real JSON
            if raw.startswith("["):
                urls_to_remove = json.loads(raw)

            # Case 2: Python list string like "['a','b']"
            elif raw.startswith("'[") or raw.startswith("["):
                urls_to_remove = ast.literal_eval(raw)

            # Case 3: comma-separated fallback
            elif "," in raw:
                urls_to_remove = [u.strip() for u in raw.split(",")]

            # Case 4: single value
            else:
                urls_to_remove = [raw]

        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid remove_attachment_urls format: {str(e)}"
            )

        print("FINAL PARSED:", urls_to_remove)

        for url in urls_to_remove:
            await delete_file(url)

        current_attachments = [
            u for u in current_attachments
            if u not in urls_to_remove
        ]

    # Upload and append new attachments
    if new_attachments:
        for file in new_attachments:
            if file.filename:
                file_url = await save_file(file, upload_to="training_attachments")
                current_attachments.append(file_url)

    event.attachments = current_attachments if current_attachments else None
    await event.save()
    return await _serialize_event(event, current_user)


@router.delete("/events/{event_id}", status_code=204, tags=["Events"])
async def delete_event(
    event_id:     uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.EVENT, "delete")),
):
    """Delete an event (admin only). Cascades to EventRegistration rows."""
    event = await Event.get_or_none(id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")
    await event.delete()

    return {"message": "Event deleted successfully."}


# ── Registration ─────────────────────────────────────────────────────────────

@router.post("/events/{event_id}/register", tags=["Events"], status_code=201)
async def register_for_event(
    event_id:     uuid.UUID,
    current_user: User = Depends(login_required),
):
    event = await Event.get_or_none(id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    if await EventRegistration.filter(event=event, user=current_user).exists():
        raise HTTPException(status_code=409, detail="Already registered for this event.")

    if event.max_attendees is not None:
        count = await EventRegistration.filter(event=event).count()
        if count >= event.max_attendees:
            raise HTTPException(status_code=409, detail="Event is at full capacity.")

    reg = await EventRegistration.create(event=event, user=current_user)
    return {
        "id":            str(reg.id),
        "event_id":      str(event.id),
        "registered_at": reg.registered_at.isoformat(),
        "message":       f"Successfully registered for '{event.title}'.",
    }


@router.delete("/events/{event_id}/register", status_code=204, tags=["Events"])
async def unregister_from_event(
    event_id:     uuid.UUID,
    current_user: User = Depends(login_required),
):
    """Cancel registration for an event."""
    reg = await EventRegistration.get_or_none(event_id=event_id, user=current_user)
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    await reg.delete()

    return {"message": "Successfully unregistered from the event."}


@router.get("/events/{event_id}/registrations", tags=["Events"])
async def list_event_registrations(
    event_id:     uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.EVENT, "view")),
):
    event = await Event.get_or_none(id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    regs = await EventRegistration.filter(event=event).prefetch_related("user")
    return {
        "event_id":      str(event.id),
        "event_title":   event.title,
        "total":         len(regs),
        "max_attendees": event.max_attendees,
        "registrations": [
            {
                "id":            str(r.id),
                "registered_at": r.registered_at.isoformat(),
                "user": {
                    "id":         str(r.user_id),
                    "first_name": r.user.first_name,
                    "last_name":  r.user.last_name,
                    "email":      r.user.email,
                },
            }
            for r in regs
        ],
    }