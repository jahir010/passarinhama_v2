from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, BackgroundTasks
from tortoise.expressions import F
from pydantic import BaseModel, field_validator
import uuid
import os
import mimetypes
from datetime import datetime
from html import escape

from app.auth import role_required, superuser_required, permission_required
from app.utils.helper_functions import log_activity, check_folder_access
from app.utils.file_manager import save_file, delete_file   

from applications.user.models import User, Role, ActivityActionType, FEATURES, UserStatus
from applications.documents.models import DocumentFolder, DocumentFolderPermission, Document, FileType
from applications.notifications.notifications import NotificationLog, NotificationPreference, NotificationType
from app.utils.send_email import send_bulk_email


router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class FolderCreate(BaseModel):
    name:          str
    parent_id:     uuid.UUID | None = None
    color_code:    str = "#FFD600"


class FolderUpdate(BaseModel):
    name:       str | None = None
    color_code: str | None = None


class DocumentUpdate(BaseModel):
    original_name: str


# ──────────────────────────────────────────────────────────────────────────────
# File type detector
# ──────────────────────────────────────────────────────────────────────────────

def _detect_file_type(filename: str, mime_type: str) -> FileType:
    """Derive FileType enum from filename extension or MIME type."""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    mapping = {
        "pdf":  FileType.PDF,
        "doc":  FileType.DOC,
        "docx": FileType.DOCX,
        "xls":  FileType.XLS,
        "xlsx": FileType.XLSX,
    }
    if ext in mapping:
        return mapping[ext]
    if mime_type.startswith("image/"):
        return FileType.IMAGE
    return FileType.OTHER


# ──────────────────────────────────────────────────────────────────────────────
# Serialisers
# ──────────────────────────────────────────────────────────────────────────────

def _serialize_folder(folder: DocumentFolder, can_upload: bool = False, children: list = None) -> dict:
    """
    Folder response shape the UI sidebar needs:
      - color_code      → folder icon colour
      - document_count  → badge on folder
      - can_upload      → show/hide upload button
      - children        → nested subfolders for tree rendering
    """
    return {
        "id":             str(folder.id),
        "name":           folder.name,
        "color_code":     folder.color_code,
        "document_count": folder.document_count,
        "parent_id":      str(folder.parent_id) if folder.parent_id else None,
        "created_at":     folder.created_at.isoformat(),
        "can_upload":     can_upload,
        "children":       children if children is not None else [],
    }


def _serialize_document(doc: Document, uploader) -> dict:
    size_kb = round(doc.file_size / 1024, 1)
    size_display = f"{size_kb} KB" if size_kb < 1024 else f"{round(size_kb / 1024, 1)} MB"

    return {
        "id":            str(doc.id),
        "original_name": doc.original_name,
        "file_type":     doc.file_type,
        "mime_type":     doc.mime_type,
        "file_size":     doc.file_size,
        "file_size_display": size_display,
        "folder_id":     str(doc.folder_id),
        "created_at":    doc.created_at.isoformat(),
        "uploaded_by": {
            "id":         str(uploader.id),
            "first_name": uploader.first_name,
            "last_name":  uploader.last_name,
        },
    }


def _serialize_document_permission(perm: DocumentFolderPermission) -> dict:
    return {
        "id": str(perm.id),
        "folder_id": str(perm.folder.id),
        "folder_name": perm.folder.name,
        "role_id": str(perm.role.id),
        "role_name": perm.role.name,
        "can_read": perm.can_read,
        "can_upload": perm.can_upload,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Depth helper
# ──────────────────────────────────────────────────────────────────────────────

async def _get_folder_depth(folder_id: uuid.UUID) -> int:
    """
    Walk up the parent chain and count levels.
    Root folders return depth=1; their children depth=2; grandchildren depth=3.
    """
    depth = 1
    current_id = folder_id
    while True:
        folder = await DocumentFolder.get_or_none(id=current_id)
        if not folder or not folder.parent_id:
            break
        depth += 1
        current_id = folder.parent_id
    return depth


def _format_french_datetime(value: datetime) -> str:
    months = {
        1: "Janvier",
        2: "Février",
        3: "Mars",
        4: "Avril",
        5: "Mai",
        6: "Juin",
        7: "Juillet",
        8: "Août",
        9: "Septembre",
        10: "Octobre",
        11: "Novembre",
        12: "Décembre",
    }
    return f"{value.day} {months[value.month]} {value.year} {value:%H:%M}"


async def _document_notification_recipients(folder_id: uuid.UUID) -> tuple[list[str], list[uuid.UUID]]:
    opted_in_ids = await NotificationPreference.opted_in_user_ids(
        NotificationType.NEW_DOCUMENT
    )
    if not opted_in_ids:
        return [], []

    users = await User.filter(
        id__in=opted_in_ids,
        status=UserStatus.ACTIVE,
        is_payment_validated=True,
        is_deleted=False,
    ).prefetch_related("role")

    emails: list[str] = []
    user_ids: list[uuid.UUID] = []
    for user in users:
        if not await user.has_permission(FEATURES.DOCUMENT, "view"):
            continue
        if not user.is_superuser:
            perm = await DocumentFolderPermission.get_or_none(
                folder_id=folder_id,
                role_id=user.role_id,
                can_read=True,
            )
            if perm is None:
                continue
        emails.append(user.email)
        user_ids.append(user.id)

    return emails, user_ids


async def _notify_new_document(
    document_id: uuid.UUID,
    folder_id: uuid.UUID,
    folder_name: str,
    file_name: str,
    uploader_name: str,
    uploaded_at: datetime,
) -> None:
    emails, user_ids = await _document_notification_recipients(folder_id)
    if not emails:
        return

    document_url = f"https://archicopro.cloud/documents/{document_id}/download"
    safe_folder_name = escape(folder_name)
    safe_file_name = escape(file_name)
    safe_uploader_name = escape(uploader_name)
    safe_uploaded_at = escape(_format_french_datetime(uploaded_at))

    html_body = f"""
    <html>
    <body style="margin:0; padding:20px; background:#ffffff; font-family:Arial,sans-serif;">
      <table width="640" cellpadding="0" cellspacing="0" border="0"
             style="border:3px solid #F5C518; margin:auto; background:#ffffff;">
        <tr>
          <td align="center" style="background-color:#F5C518; padding:12px 20px;">
            <a href="https://archicopro.cloud"
               style="color:#000000; font-size:18px; font-weight:bold;
                      text-decoration:underline; display:block; margin-bottom:6px;">
              Extranet de la Compagnie des Architectes de Copropriété
            </a>
            <span style="color:#000000; font-size:16px;">
              Un immeuble , un architecte
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:22px 34px 14px 34px;">
            <p style="font-weight:bold; font-size:16px; margin:0 0 38px 0;">
              Un nouveau fichier a été déposé
            </p>
            <table cellpadding="0" cellspacing="0" border="0" style="width:100%;">
              <tr>
                <td style="padding:4px 0; color:#333; width:150px; font-size:16px;">Répertoire</td>
                <td style="padding:4px 0; color:#333; font-size:16px;">{safe_folder_name}</td>
              </tr>
              <tr>
                <td style="padding:4px 0; color:#333; width:150px; font-size:16px;">Fichier</td>
                <td style="padding:4px 0; font-size:16px;">
                  <a href="{document_url}" style="color:#003399; text-decoration:underline;">{safe_file_name}</a>
                </td>
              </tr>
              <tr>
                <td style="padding:4px 0; color:#333; width:150px; font-size:16px;">Auteur</td>
                <td style="padding:4px 0; color:#333; font-size:16px;">{safe_uploader_name}</td>
              </tr>
              <tr>
                <td style="padding:4px 0; color:#333; width:150px; font-size:16px;">Date</td>
                <td style="padding:4px 0; color:#333; font-size:16px;">{safe_uploaded_at}</td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td align="center"
              style="border-top:1px dashed #F5C518; padding:10px;
                     font-size:12px; color:#555;">
            Powered by <a href="https://archicopro.cloud" style="color:#003399;">Archicopro</a>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    result = await send_bulk_email(
        subject=f"Un fichier a été déposé {folder_name}/{file_name}",
        recipients=emails,
        html_message=html_body,
        chunk_size=50,
        chunk_delay=1.0,
        retries=1,
    )

    if result["sent"] > 0:
        await NotificationLog.bulk_create_for_users(
            user_ids=user_ids,
            notification_type=NotificationType.NEW_DOCUMENT,
            target_type="document",
            target_id=document_id,
        )

    print(
        f"[notify] document={document_id} envoyé={result['sent']} échec={result['failed']}",
        flush=True,
    )



@router.get("/documents/folders", tags=["Documents"])
async def list_folders(
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "view"))
):
    if current_user.is_superuser:
        all_folders = await DocumentFolder.all().order_by("name")
        # Build accessible dict for superusers (all folders with upload permission)
        accessible: dict[str, tuple] = {
            str(folder.id): (folder, True) for folder in all_folders
        }
    else:
        perms = await DocumentFolderPermission.filter(
            role=current_user.role, can_read=True
        ).prefetch_related("folder")
        # Build lookup: folder_id → (folder, can_upload)
        accessible: dict[str, tuple] = {}
        for p in perms:
            accessible[str(p.folder_id)] = (p.folder, p.can_upload)
        all_folders = [f for f, _ in accessible.values()]
    
    # Group folders by parent_id for O(1) child lookup
    children_map: dict[str | None, list] = {}
    for folder in all_folders:
        key = str(folder.parent_id) if folder.parent_id else None
        children_map.setdefault(key, []).append(folder)
    
    def build_subtree(parent_id: str | None) -> list:
        folders = children_map.get(parent_id, [])
        result = []
        for folder in sorted(folders, key=lambda f: f.name):
            _, can_upload = accessible.get(str(folder.id), (None, False))
            children = build_subtree(str(folder.id))
            result.append(_serialize_folder(folder, can_upload, children))
        return result
    
    return build_subtree(None)


@router.post("/documents/folders", tags=["Documents"], status_code=201)
async def create_folder(
    body:         FolderCreate,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "create"))
):
    if body.parent_id:
        parent = await DocumentFolder.get_or_none(id=body.parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent folder not found.")

        parent_depth = await _get_folder_depth(body.parent_id)
        if parent_depth >= 10:
            raise HTTPException(
                status_code=400,
                detail="Maximum folder nesting depth of 3 levels reached. Cannot create subfolder here.",
            )

    folder = await DocumentFolder.create(**body.model_dump())


    roles = await Role.all()

    for role in roles:
        _, created = await DocumentFolderPermission.get_or_create(
            folder=folder,
            role=role
        )

    return _serialize_folder(folder, can_upload=True)


@router.patch("/documents/folders/{folder_id}", tags=["Documents"])
async def rename_folder(
    folder_id:    uuid.UUID,
    body:         FolderUpdate,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "edit"))
):
    folder = await DocumentFolder.get_or_none(id=folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(folder, field, value)
    await folder.save()

    # Check caller's upload permission for response
    perm = await DocumentFolderPermission.get_or_none(folder=folder, role=current_user.role)
    can_upload = perm.can_upload if perm else False
    return _serialize_folder(folder, can_upload)


@router.get("/documents/folders/{folder_id}/permissions", tags=["Documents"])
async def get_folder_permissions(
    folder_id:    uuid.UUID,
    current_user: User = Depends(superuser_required),
):
    folder = await DocumentFolder.get_or_none(id=folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")

    perms = await DocumentFolderPermission.filter(folder=folder).all().prefetch_related("role", "folder")
    return [_serialize_document_permission(p) for p in perms]


@router.patch("/documents/folders/{folder_id}/permissions", tags=["Documents"])
async def set_folder_permission(
    folder_id:  uuid.UUID,
    role:       uuid.UUID,
    can_read:   bool,
    can_upload: bool,
    current_user: User = Depends(superuser_required)
):
    folder = await DocumentFolder.get_or_none(id=folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")
    perm, _ = await DocumentFolderPermission.get_or_create(folder=folder, role=role)
    perm.can_read   = can_read
    perm.can_upload = can_upload
    await perm.save()
    return perm


@router.delete("/documents/folders/{folder_id}", status_code=204, tags=["Documents"])
async def delete_folder(
    folder_id:    uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "delete"))
):
    folder = await DocumentFolder.get_or_none(id=folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")
    await folder.delete()

    return {"message": "Folder deleted."}


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/documents", tags=["Documents"])
async def list_documents(
    folder_id:    uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "view"))
):
    
    folder = await DocumentFolder.get_or_none(id=folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")
    await check_folder_access(folder, current_user)

    docs = await Document.filter(folder=folder).order_by("-created_at").prefetch_related("uploaded_by")

    perm = await DocumentFolderPermission.get_or_none(folder=folder, role=current_user.role)
    can_upload = perm.can_upload if perm else False

    return {
        "folder": {
            "id":        str(folder.id),
            "name":      folder.name,
            "can_upload": can_upload,
        },
        "total": len(docs),
        "documents": [_serialize_document(d, d.uploaded_by) for d in docs],
    }


@router.post("/documents/upload", tags=["Documents"], status_code=201)
async def upload_document(
    background_tasks: BackgroundTasks,
    folder_id:    uuid.UUID   = Form(...),
    file:         UploadFile  = File(...),
    current_user: User        = Depends(permission_required(FEATURES.DOCUMENT, "create"))
):
    
    folder = await DocumentFolder.get_or_none(id=folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found.")
    await check_folder_access(folder, current_user, need_upload=True)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename.")

    # Detect MIME type — fall back to content_type from the upload if available
    mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
    file_type  = _detect_file_type(file.filename, mime_type)

    # Read size before saving (seek to end, record position, seek back)
    content    = await file.read()
    file_size  = len(content)
    await file.seek(0)

    storage_path = await save_file(file, upload_to="documents")

    doc = await Document.create(
        folder=folder,
        uploaded_by=current_user,
        filename=os.path.basename(storage_path),   # stored filename (may be UUID-renamed)
        original_name=file.filename,               # display name shown to users
        file_type=file_type,
        mime_type=mime_type,
        file_size=file_size,
        storage_path=storage_path,
    )

    # Atomic increment — no race condition on concurrent uploads
    await DocumentFolder.filter(id=folder.id).update(document_count=F("document_count") + 1)

    await log_activity(
        current_user, ActivityActionType.DOCUMENT_UPLOADED, "document", doc.id, file.filename
    )

    background_tasks.add_task(
        _notify_new_document,
        doc.id,
        folder.id,
        folder.name,
        doc.original_name,
        current_user.full_name,
        doc.created_at,
    )

    return _serialize_document(doc, current_user)


@router.patch("/documents/{document_id}", tags=["Documents"])
async def rename_document(
    document_id:  uuid.UUID,
    body:         DocumentUpdate,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "edit")),
):
    doc = await Document.get_or_none(id=document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    doc.original_name = body.original_name
    await doc.save(update_fields=["original_name"])
    return _serialize_document(doc, current_user)


@router.get("/documents/{document_id}/download", tags=["Documents"])
async def get_download_url(
    document_id:  uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "view"))
):
    doc = await Document.get_or_none(id=document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    folder = await doc.folder
    await check_folder_access(folder, current_user)

    return {
        "download_url": doc.storage_path,   # the URL returned by save_file()
        "filename":     doc.original_name,
        "mime_type":    doc.mime_type,
    }


@router.delete("/documents/{document_id}", status_code=204, tags=["Documents"])
async def delete_document(
    document_id:  uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.DOCUMENT, "delete"))
):
    
    doc = await Document.get_or_none(id=document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    folder = await doc.folder
    original_name = doc.original_name
    storage_path  = doc.storage_path   # capture before delete

    await doc.delete()

    # Remove the actual file from local storage 
    await delete_file(storage_path)

    # Atomic decrement — no race condition, no negative values
    await DocumentFolder.filter(id=folder.id).update(
        document_count=F("document_count") - 1
    )

    
    await log_activity(
        current_user, ActivityActionType.DOCUMENT_DELETED, "document", document_id, original_name
    )

    return {"message": "Document deleted."}







# ─── Pydantic schema ───────────────────────────────────────────────────────────

class BulkFolderPermissionRequest(BaseModel):
    folder_id: list[uuid.UUID]
    role_id:     list[uuid.UUID]
    can_read: bool
    can_upload: bool

    @field_validator("folder_id", "role_id")
    @classmethod
    def no_empty(cls, v):
        if not v:
            raise ValueError("List cannot be empty.")
        return v


# ─── Endpoint ──────────────────────────────────────────────────────────────────

@router.patch("/folder/permissions/bulk", tags=["Folders"])
async def set_folder_permissions_bulk(
    body:         BulkFolderPermissionRequest,
    current_user: User = Depends(superuser_required)
):
    # 1. Validate all folder IDs exist in ONE query
    found_folders = await DocumentFolder.filter(id__in=body.folder_id).only("id")
    found_ids    = {f.id for f in found_folders}

    missing = set(body.folder_id) - found_ids
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Folders not found or inactive: {[str(m) for m in missing]}",
        )

    # 2. Expand (folder_id x role) combinations
    records = [
        DocumentFolderPermission(
            folder_id = folder_id,
            role_id     = role_id,
            can_read = body.can_read,
            can_upload = body.can_upload,
        )
        for folder_id in body.folder_id
        for role_id in body.role_id
    ]

    # 3. Single upsert — one round-trip
    await DocumentFolderPermission.bulk_create(
        records,
        update_fields=["can_read", "can_upload"],
        on_conflict=["folder_id", "role_id"],
    )

    # 4. Return updated rows
    result = await DocumentFolderPermission.filter(
        folder_id__in=body.folder_id
    ).values("id", "folder_id", "role_id", "can_read", "can_upload")

    return {"updated": len(records), "permissions": result}
