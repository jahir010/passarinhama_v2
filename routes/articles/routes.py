
import ast
from typing import List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status, Query, BackgroundTasks
from tortoise.expressions import Q
from pydantic import BaseModel, field_validator
import uuid
from datetime import datetime, timezone as UTC

from app.auth import permission_required, superuser_required
from app.token import get_current_user
from app.utils.file_manager import delete_file, update_file, save_file
from app.utils.helper_functions import check_article_access
from applications.articles.models import Article, ArticleCategory, ArticleStatus, ArticleRolePermission
from applications.user.models import   User, Role, UserStatus, ActivityActionType, ActivityLog, FEATURES
from applications.notifications.notifications import NotificationType, NotificationLog, NotificationPreference
from routes.user.routes import log_activity
from app.utils.send_email import send_email, send_bulk_email
import json



router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES
# ══════════════════════════════════════════════════════════════════════════════

class ArticleCreate(BaseModel):
    title:             str
    category_id:       uuid.UUID
    excerpt:           str | None = None
    body:              str | None = None
    thumbnail_url:     str | None = None
    structured_fields: dict | None = None
    
 
class ArticleUpdate(ArticleCreate):
    title:       str | None = None
    category_id: uuid.UUID | None = None
 
class ArticleOut(BaseModel):
    id:           uuid.UUID
    title:        str
    excerpt:      str | None
    status:       ArticleStatus
    published_at: datetime | None
    created_at:   datetime
 
    class Config:
        from_attributes = True



async def _notify_new_article(article_id: uuid.UUID, article_title: str) -> None:

    opted_in_ids = await NotificationPreference.opted_in_user_ids(
        NotificationType.NEW_ARTICLE
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
        <h2>New Article Published</h2>
        <p>A new article is now available on the platform:</p>
        <p><strong>{article_title}</strong></p>
        <p>
          <a href="https://yourplatform.com/articles/{article_id}"
             style="background:#4F46E5;color:#fff;padding:10px 20px;
                    border-radius:6px;text-decoration:none;">
            Read Article
          </a>
        </p>
        <hr/>
        <small>
          You're receiving this because you subscribed to article notifications.
          <a href="https://yourplatform.com/settings/notifications">Unsubscribe</a>
        </small>
      </body>
    </html>
    """
 
    # 4. Send in chunks — respects SMTP rate limits
    result = await send_bulk_email(
        subject=f"New Article: {article_title}",
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
            notification_type=NotificationType.NEW_ARTICLE,
            target_type="article",
            target_id=article_id,
        )
 
    print(
        f"[notify] article={article_id} sent={result['sent']} failed={result['failed']}",
        flush=True,
    )


async def _article_serialize(article: Article) -> dict:
    """Convert Article model instance to dict for email content interpolation."""
    return {
        "id": article.id,
        "title": article.title,
        "excerpt": article.excerpt,
        "body": article.body,
        "thumbnail_url": article.thumbnail_url,
        "structured_fields": article.structured_fields,
        "status": article.status,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "created_at": article.created_at.isoformat(),
        "author": {
            "id": article.author.id,
            "name": article.author.first_name,
            "email": article.author.email
        },
        "category": {
            "id": article.category.id,
            "name": article.category.name
        }
    }


def _serialize_article_permission(perm: ArticleRolePermission) -> dict:
    return {
        "id": str(perm.id),
        "article_id": str(perm.article.id),
        "article_title": perm.article.title,
        "role_id": str(perm.role.id),
        "role_name": perm.role.name,
        "can_read": perm.can_read,
        "can_write": perm.can_write
    }




 
@router.get("/articles", tags=["Articles"])
async def list_articles(
    status:      ArticleStatus | None = None,
    category_id: uuid.UUID | None = None,
    search:      str | None = None,
    page:        int = Query(1, ge=1),
    page_size:   int = Query(20, ge=1, le=100),
    current_user: User | None = Depends(permission_required(FEATURES.ARTICLE, "view"))
):
    qs = Article.filter()

    if not current_user.is_superuser:
        qs = qs.filter(
            role_permissions__role=current_user.role,
            role_permissions__can_read=True,
        )
 
    
    if status == ArticleStatus.DRAFT:
        qs = qs.filter(status=status, author=current_user)
    elif status:
        qs = qs.filter(status=status)
    
    else:
        qs = qs.filter(status=ArticleStatus.PUBLISHED)
 
    if category_id:
        qs = qs.filter(category_id=category_id)
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(excerpt__icontains=search))
 
    total    = await qs.distinct().count()
    articles = await qs.distinct().offset((page - 1) * page_size).limit(page_size).prefetch_related("author", "category")
    return {"total": total, "page": page, "results": [await _article_serialize(a) for a in articles]}
 
 

@router.post("/articles", tags=["Articles"], status_code=201)
async def create_article(
    title: str = Form(...),
    category_id: uuid.UUID = Form(...),
    excerpt: str = Form(None),
    body: str = Form(None),
    status: ArticleStatus = Form(ArticleStatus.PENDING),
    thumbnail: UploadFile = File(None),
    structured_fields: str = Form(None),  # JSON string
    files: list[UploadFile] = File(None),
    current_user: User = Depends(permission_required(FEATURES.ARTICLE, "create"))
):
    category = await ArticleCategory.get_or_none(id=category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found.")

    # parse structured_fields JSON string → dict
    import json
    parsed_structured_fields = json.loads(structured_fields) if structured_fields else {}

    # handle files
    if files:
        file_urls = []
        for upload in files:
            url = await save_file(upload, upload_to="articles")
            file_urls.append(url)
        parsed_structured_fields["file_urls"] = file_urls
    
    if thumbnail is not None:
        thumbnail_url = await save_file(thumbnail, upload_to="articles")
    else:
        thumbnail_url = None

    article = await Article.create(
        title=title,
        category=category,
        excerpt=excerpt,
        body=body,
        thumbnail_url=thumbnail_url,
        structured_fields=parsed_structured_fields,
        status=status,
        author=current_user,
    )

    roles = await Role.all()

    for role in roles:
        _, created = await ArticleRolePermission.get_or_create(
            article=article,
            role=role
        )

    await log_activity(current_user, ActivityActionType.ARTICLE_PUBLISHED, "article", article.id, title)

    return article


@router.get("/my-articles", tags=["Articles"])
async def list_my_articles(
    status: ArticleStatus | None = None,
    author_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(FEATURES.ARTICLE, "view"))
):
    if author_id:
        qs = Article.filter(author_id=author_id)
    else:
        qs = Article.filter(author=current_user)

    if not current_user.is_superuser:
        qs = qs.filter(
            role_permissions__role=current_user.role,
            role_permissions__can_read=True,
        )

    if status:
        qs = qs.filter(status=status)
    total    = await qs.distinct().count()
    articles = await qs.distinct().offset((page - 1) * page_size).limit(page_size).prefetch_related("author", "category")
    return {"total": total, "page": page, "results": [await _article_serialize(a) for a in articles]}
 
 
@router.get("/articles/{article_id}", tags=["Articles"])
async def get_article(article_id: uuid.UUID, current_user: User | None = Depends(permission_required(FEATURES.ARTICLE, "view"))):
    article = await Article.get_or_none(id=article_id).prefetch_related("author", "category")
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    await check_article_access(article, current_user)
    if article.status == ArticleStatus.DRAFT:
        if current_user.id != article.author_id:
            raise HTTPException(status_code=403, detail="Draft articles are restricted.")
    return await _article_serialize(article)
 
 



@router.patch("/articles/{article_id}", tags=["Articles"])
async def update_article(
    article_id: uuid.UUID,
    title: str = Form(None),
    category_id: uuid.UUID = Form(None),
    excerpt: str = Form(None),
    body: str = Form(None),
    thumbnail: UploadFile = File(None),
    structured_fields: str = Form(None),  # JSON string
    files: list[UploadFile] = File(None),
    remove_attachment_urls: Optional[str] = Form(None),
    current_user: User = Depends(permission_required(FEATURES.ARTICLE, "edit"))
):
    article = await Article.get_or_none(id=article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    await check_article_access(article, current_user, need_write=True)

    # category update (if provided)
    if category_id:
        category = await ArticleCategory.get_or_none(id=category_id)
        if not category:
            raise HTTPException(status_code=404, detail="Category not found.")
        article.category = category

    # parse structured_fields safely
    parsed_structured_fields = article.structured_fields or {}
    if structured_fields:
        try:
            parsed_structured_fields.update(json.loads(structured_fields))
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid structured_fields JSON.")
        


    current_attachments: List[str] = article.structured_fields["file_urls"] or []
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
    if files:
        for file in files:
            if file.filename:
                file_url = await save_file(file, upload_to="articles")
                current_attachments.append(file_url)

    article.structured_fields["file_urls"] = current_attachments if current_attachments else None

   
    # update fields if provided
    if title is not None:
        article.title = title
    if excerpt is not None:
        article.excerpt = excerpt
    if body is not None:
        article.body = body
    if thumbnail is not None:
        article.thumbnail_url = await update_file(thumbnail, file_url=article.thumbnail_url, upload_to="articles")

    article.structured_fields = parsed_structured_fields

    await article.save()

    await log_activity(
        current_user,
        ActivityActionType.ARTICLE_UPDATED,
        "article",
        article.id,
        article.title,
    )

    return article
 
 
@router.post("/articles/{article_id}/publish", tags=["Articles"])
async def publish_article(
    article_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(permission_required(FEATURES.ARTICLE, "edit"))
):
    """Toggle article status between draft and published."""
    article = await Article.get_or_none(id=article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    await check_article_access(article, current_user, need_write=True)
    if article.status == ArticleStatus.PENDING:
        article.status       = ArticleStatus.PUBLISHED
        article.published_at = datetime.now(UTC.utc)
        await article.save(update_fields=["status", "published_at"])
        try:
            background_tasks.add_task(_notify_new_article, article.id, article.title)
        except Exception as e:
            print(f"[notify] Failed to enqueue notification task: {e}", flush=True)
    else:
        article.status = ArticleStatus.PENDING
        await article.save(update_fields=["status"])
    return {"status": article.status}
 
 
@router.delete("/articles/{article_id}", status_code=204, tags=["Articles"])
async def delete_article(article_id: uuid.UUID, current_user: User = Depends(permission_required(FEATURES.ARTICLE, "delete"))):
    article = await Article.get_or_none(id=article_id)
    if article:
        await check_article_access(article, current_user, need_write=True)
    if article and article.structured_fields.get("file_urls", []):
        for url in article.structured_fields.get("file_urls", []):
            print(f"Deleting file from article deletion: {url}", flush=True)
            await delete_file(url)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    await article.delete()

    return "delete success"
 
 
# ── Article Categories ────────────────────
 
@router.get("/article-categories", tags=["Articles"])
async def list_article_categories(current_user: User = Depends(permission_required(FEATURES.ARTICLE, "view"))):
    return await ArticleCategory.all()
 
 
@router.post("/article-categories", tags=["Articles"], status_code=201)
async def create_article_category(name: str, color_code: str = "#FFD600", current_user: User = Depends(permission_required(FEATURES.ARTICLE, "create"))):
    return await ArticleCategory.create(name=name, color_code=color_code)


@router.patch("/article-categories/{category_id}", tags=["Articles"], status_code=201)
async def update_article_category(category_id: uuid.UUID, name: str = None, color_code: str = None, current_user: User = Depends(permission_required(FEATURES.ARTICLE, "edit"))):
    category = await ArticleCategory.get_or_none(id=category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Article category not found.")
    if name is not None:
        category.name = name
    if color_code is not None:
        category.color_code = color_code
    await category.save()
    return category


@router.delete("/article-categories/{category_id}", tags=["Articles"], status_code=204)
async def delete_article_category(category_id: uuid.UUID, current_user: User = Depends(permission_required(FEATURES.ARTICLE, "delete"))):
    category = await ArticleCategory.get_or_none(id=category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Article category not found.")
    if await Article.filter(category=category).exists():
        raise HTTPException(status_code=400, detail="Cannot delete category with associated articles.")
    await category.delete()
    return "delete success"






@router.post("/file-upload", tags=["Articles"])
async def upload_file(file: UploadFile = File(...),
                      current_user: User = Depends(get_current_user)):
    url = await save_file(file, upload_to="article_files")
    return {"url": url}


@router.delete("/file-delete", tags=["Articles"])
async def delete_file_endpoint(file_url: List[str] = Form(...),
                             current_user: User = Depends(get_current_user)):
    for url in file_url:
        await delete_file(url)
    return {"deleted": file_url}



# ─── Pydantic schema ───────────────────────────────────────────────────────────

class BulkArticlePermissionRequest(BaseModel):
    article_id: list[uuid.UUID]
    role_id:     list[uuid.UUID]
    can_read: bool
    can_write: bool

    @field_validator("article_id", "role_id")
    @classmethod
    def no_empty(cls, v):
        if not v:
            raise ValueError("List cannot be empty.")
        return v



@router.patch("/article/permissions/bulk", tags=["Articles"])
async def set_article_permissions_bulk(
    body:         BulkArticlePermissionRequest,
    current_user: User = Depends(superuser_required)
):
    # 1. Validate all article IDs exist in ONE query
    found_articles = await Article.filter(id__in=body.article_id).only("id")
    found_ids    = {f.id for f in found_articles}

    missing = set(body.article_id) - found_ids
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Articles not found or inactive: {[str(m) for m in missing]}",
        )

    # 2. Expand (article_id x role) combinations
    records = [
        ArticleRolePermission(
            article_id = article_id,
            role_id     = role_id,
            can_read = body.can_read,
            can_write = body.can_write,
        )
        for article_id in body.article_id
        for role_id in body.role_id
    ]

    # 3. Single upsert — one round-trip
    await ArticleRolePermission.bulk_create(
        records,
        update_fields=["can_read", "can_write"],
        on_conflict=["article_id", "role_id"],
    )

    # 4. Return updated rows
    result = await ArticleRolePermission.filter(
        article_id__in=body.article_id
    ).values("id", "article_id", "role_id", "can_read", "can_write")

    return {"updated": len(records), "permissions": result}


@router.get("/articles/{article_id}/permissions", tags=["Articles"])
async def get_article_permissions(
    article_id: uuid.UUID,
    current_user: User = Depends(superuser_required)
):
    article = await Article.get_or_none(id=article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    perms = await ArticleRolePermission.filter(article=article).all().prefetch_related("role", "article")
    return [_serialize_article_permission(p) for p in perms]
