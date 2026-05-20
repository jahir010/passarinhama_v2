from enum import Enum
import uuid

from passlib.context import CryptContext
from tortoise import fields, models




class FileType(str, Enum):
    PDF   = "pdf"
    DOC   = "doc"
    DOCX  = "docx"
    XLS   = "xls"
    XLSX  = "xlsx"
    IMAGE = "image"
    OTHER = "other"



# ─────────────────────────────────────────
# 19. DocumentFolder
# ─────────────────────────────────────────
 
class DocumentFolder(models.Model):
    id             = fields.UUIDField(pk=True, default=uuid.uuid4)
    name           = fields.CharField(max_length=255)
    color_code     = fields.CharField(max_length=7, default="#FFD600")
    parent         = fields.ForeignKeyField(
        "models.DocumentFolder",
        related_name="children",
        null=True,
        on_delete=fields.CASCADE,
    )
    document_count = fields.IntField(default=0)   # denormalised counter
    created_at     = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table    = "document_folders"
        ordering = ["name"]
 
    def __str__(self) -> str:
        return self.name
 
 
# ─────────────────────────────────────────
# 20. DocumentFolderPermission
# ─────────────────────────────────────────
 
class DocumentFolderPermission(models.Model):
    """Per-folder per-role read/upload access control."""
    id         = fields.UUIDField(pk=True, default=uuid.uuid4)
    folder     = fields.ForeignKeyField("models.DocumentFolder", related_name="permissions", on_delete=fields.CASCADE)
    role       = fields.ForeignKeyField("models.Role", related_name="document_permissions", on_delete=fields.CASCADE)
    can_read   = fields.BooleanField(default=False)
    can_upload = fields.BooleanField(default=False)
 
    class Meta:
        table           = "document_folder_permissions"
        unique_together = [("folder", "role")]
 
 
# ─────────────────────────────────────────
# 21. Document
# ─────────────────────────────────────────
 
class Document(models.Model):
    id            = fields.UUIDField(pk=True, default=uuid.uuid4)
    folder        = fields.ForeignKeyField("models.DocumentFolder", related_name="documents", on_delete=fields.CASCADE)
    uploaded_by   = fields.ForeignKeyField("models.User", related_name="documents", on_delete=fields.RESTRICT)
    filename      = fields.CharField(max_length=500)        # stored name in object storage
    original_name = fields.CharField(max_length=500)        # display name
    file_type     = fields.CharEnumField(FileType, default=FileType.OTHER)
    mime_type     = fields.CharField(max_length=100)
    file_size     = fields.IntField()                        # bytes
    storage_path  = fields.CharField(max_length=1000)       # object storage key — never expose
    created_at    = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table    = "documents"
        ordering = ["-created_at"]
 
    def __str__(self) -> str:
        return self.original_name
 