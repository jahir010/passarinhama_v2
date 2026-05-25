from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, File, Form, UploadFile
from typing import List, Optional
from pydantic import BaseModel, field_validator, model_validator
import uuid
from datetime import date, datetime
import json
import ast

from app.auth import permission_required, superuser_required
from app.token import get_current_user
from app.utils.helper_functions import log_activity
from app.utils.file_manager import save_file, delete_file, update_file
from app.utils.helper_functions import check_training_access

from applications.trainings.models import Training, TrainingFormat, TrainingStatus, TrainingRegistration, TrainingRolePermission
from applications.user.models import User, Role, ActivityActionType, UserStatus, FEATURES
from applications.notifications.notifications import NotificationLog, NotificationType, NotificationPreference
from app.utils.send_email import send_bulk_email, send_email


router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class TrainingCreate(BaseModel):
    title:          str
    description:    str | None         = None
    format:         TrainingFormat     = TrainingFormat.ONLINE
    training_date:  str | None         = None   # "YYYY-MM-DD"
    duration_hours: int | None         = None
    max_attendees:  int | None         = None

    @field_validator("training_date")
    @classmethod
    def parse_training_date(cls, v: str | None) -> date | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError("training_date must be YYYY-MM-DD format.")


class TrainingUpdate(BaseModel):
    """All fields optional — proper PATCH semantics."""
    title:          str | None         = None
    description:    str | None         = None
    format:         TrainingFormat | None = None
    training_date:  str | None         = None
    duration_hours: int | None         = None
    max_attendees:  int | None         = None
    status:         TrainingStatus | None = None   

    @field_validator("training_date")
    @classmethod
    def parse_training_date(cls, v: str | None) -> date | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise ValueError("training_date must be YYYY-MM-DD format.")


# ──────────────────────────────────────────────────────────────────────────────
# Shared serialiser
# ──────────────────────────────────────────────────────────────────────────────




async def _notify_new_training(training_id: uuid.UUID, training_title: str) -> None:
    
    # 1. Resolve opted-in user IDs (excludes explicit opt-outs)
    opted_in_ids = await NotificationPreference.opted_in_user_ids(
        NotificationType.NEW_TRAINING
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
        <h2>New Training Published</h2>
        <p>A new training is now available on the platform:</p>
        <p><strong>{training_title}</strong></p>
        <p>
          <a href="https://yourplatform.com/trainings/{training_id}"
             style="background:#4F46E5;color:#fff;padding:10px 20px;
                    border-radius:6px;text-decoration:none;">
            Read Training
          </a>
        </p>
        <hr/>
        <small>
          You're receiving this because you subscribed to training notifications.
          <a href="https://yourplatform.com/settings/notifications">Unsubscribe</a>
        </small>
      </body>
    </html>
    """
 
    # 4. Send in chunks — respects SMTP rate limits
    result = await send_bulk_email(
        subject=f"New Training: {training_title}",
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
            notification_type=NotificationType.NEW_TRAINING,
            target_type="training",
            target_id=training_id,
        )
 
    print(
        f"[notify] training={training_id} sent={result['sent']} failed={result['failed']}",
        flush=True,
    )

async def _serialize_training(training: Training, current_user: User | None = None) -> dict:
    
    attendee_count = await TrainingRegistration.filter(training=training).count()

    is_registered = False
    if current_user:
        is_registered = await TrainingRegistration.filter(
            training=training, user=current_user
        ).exists()

    spots_left = None
    if training.max_attendees is not None:
        spots_left = max(0, training.max_attendees - attendee_count)

   
    effective_status = training.status
    if (
        training.training_date
        and training.training_date < date.today()
        and training.status != TrainingStatus.COMPLETED
    ):
        effective_status = TrainingStatus.COMPLETED

    created_by = await training.created_by
    return {
        "id":             str(training.id),
        "title":          training.title,
        "description":    training.description,
        "instructor_name": training.instructor_name,
        "format":         training.format,
        "training_date":  training.training_date.isoformat() if training.training_date else None,
        "end_date":       training.end_date.isoformat() if training.end_date else None,
        "duration_hours": training.duration_hours,
        "thumbnail_url":  training.thumbnail_url,
        "max_attendees":  training.max_attendees,
        "attendee_count": attendee_count,
        "spots_left":     spots_left,
        "is_at_capacity": spots_left == 0 if spots_left is not None else False,
        "is_registered":  is_registered,
        "status":         effective_status,
        "attachments":    training.attachments or [],
        "created_at":     training.created_at.isoformat(),
        "created_by": {
            "id":         str(created_by.id),
            "first_name": created_by.first_name,
            "last_name":  created_by.last_name,
        },
    }



def _serialize_training_permission(perm: TrainingRolePermission) -> dict:
    return {
        "id": str(perm.id),
        "training_id": str(perm.training.id),
        "training_name": perm.training.title,
        "role_id": str(perm.role.id),
        "role_name": perm.role.name,
        "can_read": perm.can_read,
        "can_write": perm.can_write
    }


# ══════════════════════════════════════════════════════════════════════════════
# TRAININGS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/trainings/upcoming", tags=["Trainings"])
async def upcoming_trainings(
    page:         int  = Query(1, ge=1),
    page_size:    int  = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(FEATURES.TRAINING, "view"))
):
    
    today = date.today()
    qs = Training.filter(training_date__gte=today)
    if not current_user.is_superuser:
        qs = qs.filter(
            role_permissions__role=current_user.role,
            role_permissions__can_read=True,
        )
    total = await qs.distinct().count()
    items = await qs.distinct().order_by("training_date").offset((page - 1) * page_size).limit(page_size)
    return {
        "total":   total,
        "page":    page,
        "results": [await _serialize_training(t, current_user) for t in items],
    }


@router.get("/trainings/past", tags=["Trainings"])
async def past_trainings(
    page:         int  = Query(1, ge=1),
    page_size:    int  = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(FEATURES.TRAINING, "view")),
):
    today = date.today()
    qs = Training.filter(training_date__lt=today)
    if not current_user.is_superuser:
        qs = qs.filter(
            role_permissions__role=current_user.role,
            role_permissions__can_read=True,
        )
    total = await qs.distinct().count()
    items = await qs.distinct().order_by("-training_date").offset((page - 1) * page_size).limit(page_size)
    return {
        "total":   total,
        "page":    page,
        "results": [await _serialize_training(t, current_user) for t in items],
    }


@router.get("/trainings/dashboard-widget", tags=["Trainings"])
async def trainings_dashboard_widget(
    current_user: User = Depends(permission_required(FEATURES.TRAINING, "view")),
):
    today = date.today()
    qs = Training.filter(training_date__gte=today)
    if not current_user.is_superuser:
        qs = qs.filter(
            role_permissions__role=current_user.role,
            role_permissions__can_read=True,
        )
    items = await qs.distinct().order_by("training_date").limit(4)
    return [await _serialize_training(t, current_user) for t in items]


@router.get("/trainings", tags=["Trainings"])
async def list_trainings(
    status:       TrainingStatus | None = None,
    current_user: User                  = Depends(permission_required(FEATURES.TRAINING, "view"))
):
    qs = Training.filter()
    if not current_user.is_superuser:
        qs = qs.filter(
            role_permissions__role=current_user.role,
            role_permissions__can_read=True,
        )
    if status:
        qs = qs.filter(status=status)
    items = await qs.distinct().order_by("training_date")
    return [await _serialize_training(t, current_user) for t in items]


@router.get("/trainings/{training_id}", tags=["Trainings"])
async def get_training(
    training_id:  uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.TRAINING, "view"))
):
   
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")
    await check_training_access(training, current_user)
    return await _serialize_training(training, current_user)


@router.post("/trainings", tags=["Trainings"], status_code=201)
async def create_training(
    background_tasks: BackgroundTasks,
    current_user:     User              = Depends(permission_required(FEATURES.TRAINING, "create")),
    title:            str               = Form(...),
    description:      Optional[str]     = Form(None),
    instructor_name:  Optional[str]     = Form(None),
    format:           TrainingFormat    = Form(TrainingFormat.ONLINE),
    training_date:    Optional[str]     = Form(None),   # "YYYY-MM-DD"
    end_date:         Optional[str]     = Form(None),   # "YYYY-MM-DD"
    duration_hours:   Optional[int]     = Form(None),
    max_attendees:    Optional[int]     = Form(None),
    thumbnail:        Optional[UploadFile] = File(None),
    attachments:      List[UploadFile]  = File(default=[]),
):
    
    # Coerce and validate date
    parsed_date: Optional[date] = None
    parsed_end_date: Optional[date] = None
    if training_date:
        try:
            parsed_date = date.fromisoformat(training_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="training_date must be YYYY-MM-DD format.")

    if end_date:
        try:
            parsed_end_date = date.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="end_date must be YYYY-MM-DD format.")

    # Upload attachments
    attachment_urls: List[str] = []
    for file in attachments:
        if file.filename:
            file_url = await save_file(file, upload_to="training_attachments")
            attachment_urls.append(file_url)

    # Upload thumbnail
    thumbnail_url: Optional[str] = None
    if thumbnail:
        thumbnail_url = await save_file(thumbnail, upload_to="training_thumbnails")

    training = await Training.create(
        title=title,
        description=description,
        format=format,
        training_date=parsed_date,
        end_date=parsed_end_date,
        duration_hours=duration_hours,
        thumbnail_url=thumbnail_url,
        max_attendees=max_attendees,
        attachments=attachment_urls if attachment_urls else None,
        created_by=current_user,
        instructor_name=instructor_name,
    )

    roles = await Role.all()

    for role in roles:
        _, created = await TrainingRolePermission.get_or_create(
            training=training,
            role=role
        )
    
    await log_activity(
        current_user, ActivityActionType.TRAINING_CREATED, "training", training.id, title
    )

    # FIX: respect notification preferences — do NOT blast all active users
    prefs = await NotificationPreference.filter(
        notification_type=NotificationType.NEW_TRAINING,
        email_enabled=True,
    ).prefetch_related("user")

    try:
        background_tasks.add_task(_notify_new_training, training.id, training.title)
    except Exception as e:
        print(f"[notify] Failed to enqueue notification task: {e}", flush=True)

    return await _serialize_training(training, current_user)


@router.patch("/trainings/{training_id}", tags=["Trainings"])
async def update_training(
    training_id:           uuid.UUID,
    current_user:          User                = Depends(permission_required(FEATURES.TRAINING, "edit")),
    title:                 Optional[str]        = Form(None),
    description:           Optional[str]        = Form(None),
    instructor_name:       Optional[str]        = Form(None), 
    format:                Optional[TrainingFormat] = Form(None),
    training_date:         Optional[str]        = Form(None),   # "YYYY-MM-DD"
    end_date:              Optional[str]        = Form(None),   # "YYYY-MM-DD"
    duration_hours:        Optional[int]        = Form(None),
    max_attendees:         Optional[int]        = Form(None),
    thumbnail:             Optional[UploadFile] = File(None),
    status:                Optional[TrainingStatus] = Form(None),
    new_attachments:       Optional[List[UploadFile]] = File(default=None),
    remove_attachment_urls: Optional[str]       = Form(None),   # JSON array of URLs to remove
):
    
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")
    await check_training_access(training, current_user, need_write=True)

    # --- scalar fields (only update what was provided) ---
    if title is not None:
        training.title = title
    if description is not None:
        training.description = description
    if instructor_name is not None:
        training.instructor_name = instructor_name
    if format is not None:
        training.format = format
    if training_date is not None:
        try:
            training.training_date = date.fromisoformat(training_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="training_date must be YYYY-MM-DD format.")
    if end_date is not None:
        try:
            training.end_date = date.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="end_date must be YYYY-MM-DD format.")
    if duration_hours is not None:
        training.duration_hours = duration_hours
    if max_attendees is not None:
        training.max_attendees = max_attendees
    if status is not None:
        training.status = status
    if thumbnail is not None:
        # Upload new thumbnail and update URL
        thumbnail_url = await update_file(
            new_file=thumbnail,
            file_url=training.thumbnail_url,
            upload_to="training_thumbnails"
        )
        training.thumbnail_url = thumbnail_url

    # --- attachment management ---
    current_attachments: List[str] = training.attachments or []

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

    training.attachments = current_attachments if current_attachments else None

    await training.save()
    return await _serialize_training(training, current_user)


@router.delete("/trainings/{training_id}", status_code=204, tags=["Trainings"])
async def delete_training(
    training_id:  uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.TRAINING, "delete"))
):
   
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")
    await check_training_access(training, current_user, need_write=True)
    await training.delete()


# ── Registration ─────────────────────────────────────────────────────────────

@router.post("/trainings/{training_id}/register", tags=["Trainings"], status_code=201)
async def register_for_training(
    training_id:      uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user:     User = Depends(permission_required(FEATURES.TRAINING, "view"))
):
    
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")
    await check_training_access(training, current_user)

    # Treat past trainings as completed even if DB hasn't been updated yet
    if training.training_date and training.training_date < date.today():
        raise HTTPException(status_code=409, detail="Training is already completed.")

    if training.status != TrainingStatus.OPEN:
        raise HTTPException(status_code=409, detail=f"Training is {training.status}. Registration is closed.")

    if await TrainingRegistration.filter(training=training, user=current_user).exists():
        raise HTTPException(status_code=409, detail="Already registered for this training.")

    reg = await TrainingRegistration.create(training=training, user=current_user)
    await log_activity(current_user, ActivityActionType.TRAINING_REGISTERED, "training", training.id)

    # Auto-flip to FULL when capacity is reached
    if training.max_attendees is not None:
        count = await TrainingRegistration.filter(training=training).count()
        if count >= training.max_attendees:
            training.status = TrainingStatus.FULL
            await training.save(update_fields=["status"])

    return {
        "id":            str(reg.id),
        "training_id":   str(training.id),
        "registered_at": reg.registered_at.isoformat(),
        "message":       f"Successfully registered for '{training.title}'.",
    }


@router.delete("/trainings/{training_id}/register", status_code=204, tags=["Trainings"])
async def unregister_from_training(
    training_id:  uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.TRAINING, "view"))
):
    
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")
    await check_training_access(training, current_user)

    reg = await TrainingRegistration.get_or_none(training_id=training_id, user=current_user)
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found.")
    await reg.delete()

    # Re-open if was full — a spot just became available
    if training.status == TrainingStatus.FULL:
        training.status = TrainingStatus.OPEN
        await training.save(update_fields=["status"])


@router.get("/trainings/{training_id}/registrations", tags=["Trainings"])
async def list_training_registrations(
    training_id:  uuid.UUID,
    current_user: User = Depends(superuser_required)
):
    
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")

    regs = await TrainingRegistration.filter(training=training).prefetch_related("user")
    return {
        "training_id":    str(training.id),
        "training_title": training.title,
        "total":          len(regs),
        "max_attendees":  training.max_attendees,
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



# ─── Pydantic schema ───────────────────────────────────────────────────────────

class BulkTrainingPermissionRequest(BaseModel):
    training_id: list[uuid.UUID]
    role_id:     list[uuid.UUID]
    can_read: bool
    can_write: bool

    @field_validator("training_id", "role_id")
    @classmethod
    def no_empty(cls, v):
        if not v:
            raise ValueError("List cannot be empty.")
        return v



@router.patch("/training/permissions/bulk", tags=["Training"])
async def set_training_permissions_bulk(
    body:         BulkTrainingPermissionRequest,
    current_user: User = Depends(superuser_required)
):
    # 1. Validate all training IDs exist in ONE query
    found_trainings = await Training.filter(id__in=body.training_id).only("id")
    found_ids    = {f.id for f in found_trainings}

    missing = set(body.training_id) - found_ids
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Trainings not found or inactive: {[str(m) for m in missing]}",
        )

    # 2. Expand (training_id x role) combinations
    records = [
        TrainingRolePermission(
            training_id = training_id,
            role_id     = role_id,
            can_read = body.can_read,
            can_write = body.can_write,
        )
        for training_id in body.training_id
        for role_id in body.role_id
    ]

    # 3. Single upsert — one round-trip
    await TrainingRolePermission.bulk_create(
        records,
        update_fields=["can_read", "can_write"],
        on_conflict=["training_id", "role_id"],
    )

    # 4. Return updated rows
    result = await TrainingRolePermission.filter(
        training_id__in=body.training_id
    ).values("id", "training_id", "role_id", "can_read", "can_write")

    return {"updated": len(records), "permissions": result}


@router.get("/trainings/{training_id}/permissions", tags=["Training"])
async def get_training_permissions(
    training_id: uuid.UUID,
    current_user: User = Depends(superuser_required)
):
    training = await Training.get_or_none(id=training_id)
    if not training:
        raise HTTPException(status_code=404, detail="Training not found.")
    perms = await TrainingRolePermission.filter(training=training).all().prefetch_related("role", "training")
    return [_serialize_training_permission(p) for p in perms]
