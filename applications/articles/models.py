from enum import Enum
import uuid

from passlib.context import CryptContext
from tortoise import fields, models



class ArticleStatus(str, Enum):
    DRAFT     = "draft"
    PENDING   = "pending"
    PUBLISHED = "published"

    
# ─────────────────────────────────────────
# 7. ArticleCategory
# ─────────────────────────────────────────
 
class ArticleCategory(models.Model):
    id         = fields.UUIDField(pk=True, default=uuid.uuid4)
    name       = fields.CharField(max_length=100, unique=True)
    color_code = fields.CharField(max_length=7, default="#FFD600")  # hex
    created_at = fields.DatetimeField(auto_now_add=True)
 
    class Meta:
        table    = "article_categories"
        ordering = ["name"]
 
    def __str__(self) -> str:
        return self.name
 
 
# ─────────────────────────────────────────
# 8. Article
# ─────────────────────────────────────────
 
class Article(models.Model):
    id               = fields.UUIDField(pk=True, default=uuid.uuid4)
    title            = fields.CharField(max_length=500)
    category         = fields.ForeignKeyField("models.ArticleCategory", related_name="articles", on_delete=fields.RESTRICT)
    excerpt          = fields.TextField(null=True)
    body             = fields.TextField(null=True)           # rich text / HTML
    thumbnail_url    = fields.CharField(max_length=500, null=True)  # object storage key
    structured_fields = fields.JSONField(null=True)          # ACF-equivalent extra fields
    status           = fields.CharEnumField(ArticleStatus, default=ArticleStatus.PENDING)
    author           = fields.ForeignKeyField("models.User", related_name="articles", on_delete=fields.RESTRICT)
    published_at     = fields.DatetimeField(null=True)
    created_at       = fields.DatetimeField(auto_now_add=True)
    updated_at       = fields.DatetimeField(auto_now=True)
 
    class Meta:
        table    = "articles"
        ordering = ["-published_at", "-created_at"]
 
    def __str__(self) -> str:
        return self.title