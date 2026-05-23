from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, BackgroundTasks

from tortoise.expressions import Q, F
from pydantic import BaseModel, Field, field_validator
import uuid
import re
from datetime import datetime, timezone as UTC

from app.auth import  permission_required, superuser_required
from app.utils.helper_functions import check_forum_access, log_activity
from applications.forums.models import (
    Forum, ForumRolePermission, ModerationStatus, ModerationAction,
    Topic, Post, ModerationLog,
)
from applications.user.models import (
    FEATURES, User, Role, ActivityActionType, UserStatus,
)
from applications.notifications.notifications import NotificationLog, NotificationPreference, NotificationType
from app.utils.send_email import send_bulk_email, send_email
from app.utils.file_manager import save_file, update_file, delete_file


router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class TopicCreate(BaseModel):
    title: str


class PostCreate(BaseModel):
    content: str


class PostModerate(BaseModel):
    action:           ModerationAction
    rejection_reason: str | None = None
    forward_to:       uuid.UUID | None = None   # for FORWARD action


# ──────────────────────────────────────────────────────────────────────────────
# Helper: safe slug generator
# ──────────────────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Lower-case, replace spaces/special chars with hyphens, collapse runs."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)        # strip special characters
    slug = re.sub(r"[\s_]+", "-", slug)          # spaces/underscores → hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")   # collapse duplicate hyphens
    return slug


# ══════════════════════════════════════════════════════════════════════════════
# FORUMS
# ══════════════════════════════════════════════════════════════════════════════

async def _notify_new_post(post_id: uuid.UUID, post_title: str) -> None:
    """
    Background task: send NEW_POST emails only to users who:
      - are active, payment-validated, and not deleted
      - have NOT opted out of NEW_POST notifications
 
    Sends in chunks of 50 to stay within SMTP rate limits, then writes
    a single-batch audit log so the NotificationLog table stays accurate.
    """
    # 1. Resolve opted-in user IDs (excludes explicit opt-outs)
    opted_in_ids = await NotificationPreference.opted_in_user_ids(
        NotificationType.NEW_POST
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
        <h2>New Post Published</h2>
        <p>A new post is now available on the platform:</p>
        <p><strong>{post_title}</strong></p>
        <p>
          <a href="https://yourplatform.com/posts/{post_id}"
             style="background:#4F46E5;color:#fff;padding:10px 20px;
                    border-radius:6px;text-decoration:none;">
            Read Post
          </a>
        </p>
        <hr/>
        <small>
          You're receiving this because you subscribed to post notifications.
          <a href="https://yourplatform.com/settings/notifications">Unsubscribe</a>
        </small>
      </body>
    </html>
    """
 
    # 4. Send in chunks — respects SMTP rate limits
    result = await send_bulk_email(
        subject=f"New Post: {post_title}",
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
            notification_type=NotificationType.NEW_POST,
            target_type="post",
            target_id=post_id,
        )
 
    print(
        f"[notify] post={post_id} sent={result['sent']} failed={result['failed']}",
        flush=True,
    )


async def _notify_post_rejection(email: str) -> None:
    try:
        await send_email(subject="Your post was rejected", to=email, html_message=f"""
            <html><body>
                <p>Your post has been rejected.</p>
            </body></html>
        """)
    except Exception as e:
        print(f"[notify] Failed to send rejection email: {e}", flush=True)





@router.get("/forums", tags=["Forums"])
async def list_forums(current_user: User = Depends(permission_required(FEATURES.FORUM, "view"))):
    """
    Return all forums the current user has at least read access to,
    with stats needed by the forum list UI (topic count, last activity).

    Response shape:
      [
        {
          "id", "name", "slug", "description", "forum_type",
          "topic_count": int,
          "last_activity_at": datetime | null,
          "can_post": bool          ← so UI can hide the 'New Topic' button
        }, ...
      ]
    Spec ref: §3.2 Forum Access Matrix, §7.1
    """
    if current_user.is_superuser:
        forums = await Forum.all().prefetch_related("role_permissions")  # superusers see all forums
        result = []
        for forum in forums:
            topic_count = await Topic.filter(forum=forum).count()
            post_count = await Post.filter(topic__forum=forum).count()
            last_topic  = await Topic.filter(forum=forum).order_by("-last_activity_at").first()
            result.append({
                "id":               str(forum.id),
                "name":             forum.name,
                "slug":             forum.slug,
                "author_name":      forum.author_name,
                "description":      forum.description,
                "forum_type":       forum.forum_type,
                "topic_count":      topic_count,
                "post_count":       post_count,
                "last_activity_at": last_topic.last_activity_at if last_topic else None,
                "can_post":         True,  # superusers can post in all forums
            })
        return result
    else:
        perms = await ForumRolePermission.filter(
            role=current_user.role, can_read=True
        ).prefetch_related("forum")

        result = []
        for p in perms:
            forum = p.forum
            topic_count = await Topic.filter(forum=forum).count()
            post_count = await Post.filter(topic__forum=forum).count()
            last_topic  = await Topic.filter(forum=forum).order_by("-last_activity_at").first()
            result.append({
                "id":               str(forum.id),
                "name":             forum.name,
                "slug":             forum.slug,
                "author_name":      forum.author_name,
                "description":      forum.description,
                "forum_type":       forum.forum_type,
                "topic_count":      topic_count,
                "post_count":       post_count,
                "last_activity_at": last_topic.last_activity_at if last_topic else None,
                "can_post":         p.can_post,
            })
        return result


@router.post("/forums", tags=["Forums"], status_code=201)
async def create_forum(
    name:        str,
    author_name: str | None = None,
    description: str | None = None,
    forum_type:  str = "general",
    current_user: User = Depends(permission_required(FEATURES.FORUM, "create")),
):
    """
    Create a new forum (admin only).

    FIX: slug is now sanitised — special characters are stripped so the
    unique constraint is never violated by names like 'Board & Bureau'.
    Spec ref: §7.1, §15.5
    """
    slug = _slugify(name)

    # Guarantee uniqueness — append a counter if slug already exists
    if await Forum.filter(slug=slug).exists():
        count = await Forum.filter(slug__startswith=slug).count()
        slug = f"{slug}-{count}"

    forum = await Forum.create(
        name=name, slug=slug, description=description, forum_type=forum_type, author_name=author_name
    )

    roles = await Role.all()

    for role in roles:
        _, created = await ForumRolePermission.get_or_create(
            forum=forum,
            role=role
        )

    return forum


@router.get("/forums/{forum_id}", tags=["Forums"])
async def get_forum(
    forum_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Return a single forum with its permission details.
    403 if the user's role lacks read access.
    Spec ref: §7.1
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    await check_forum_access(forum, current_user)
    return forum


@router.patch("/forums/{forum_id}", tags=["Forums"])
async def update_forum(
    forum_id: uuid.UUID,
    name: str | None = None,
    author_name: str | None = None,
    description: str | None = None,
    forum_type: str | None = None,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "edit")),
):
    """
    Update a forum's details (admin only).
    Spec ref: §7.1, §15.5
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    if name:
        forum.name = name
        # Update slug if name changes, using the same logic as creation
        slug = _slugify(name)
        if await Forum.filter(slug=slug).exclude(id=forum_id).exists():
            count = await Forum.filter(slug__startswith=slug).exclude(id=forum_id).count()
            slug = f"{slug}-{count}"
        forum.slug = slug
    if description is not None:
        forum.description = description
    if forum_type is not None:
        forum.forum_type = forum_type
    if author_name is not None:
        forum.author_name = author_name
    await forum.save()
    await log_activity(current_user, ActivityActionType.FORUM_UPDATED, "forum", forum.id)
    return forum

@router.delete("/forums/{forum_id}", tags=["Forums"], status_code=204)
async def delete_forum(
    forum_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "delete")),
):
    """
    Delete a forum and all its topics/posts (admin only).
    Spec ref: §7.1, §15.5
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    await log_activity(current_user, ActivityActionType.FORUM_DELETED, "forum", forum.id)
    await forum.delete()
    return {"status": "Forum deleted successfully."}


@router.patch("/forums/{forum_id}/permissions", tags=["Forums"])
async def set_forum_permission(
    forum_id: uuid.UUID,
    role_id:     uuid.UUID,
    can_read: bool,
    can_post: bool,
    current_user: User = Depends(superuser_required),
):
    """
    Set or update read/post access for a given role on a forum (admin only).
    Uses get_or_create so calling this endpoint is idempotent.
    Spec ref: §15.5
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    role = await Role.get_or_none(id=role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")
    perm, _ = await ForumRolePermission.get_or_create(forum=forum, role=role)
    perm.can_read = can_read
    perm.can_post = can_post
    await perm.save()
    return perm


@router.get("/forums/{forum_id}/permissions", tags=["Forums"])
async def get_forum_permissions(
    forum_id: uuid.UUID,
    current_user: User = Depends(superuser_required),
):
    """
    Get the read/post permissions for all roles on a forum (admin only).
    Spec ref: §15.5
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    perms = await ForumRolePermission.filter(forum=forum).all()
    return perms


# ── Topics ─────────────────────────────────────────────────────────────────

@router.get("/forums/{forum_id}/topics", tags=["Forums"])
async def list_topics(
    forum_id:  uuid.UUID,
    page:      int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Paginated list of topics in a forum.

    FIX: pinned topics are sorted to the top, then by last_activity_at desc,
    matching the spec (§7.2) and the UI expectation.

    Response shape:
      {
        "total": int,
        "page":  int,
        "results": [
          {
            "id", "title", "is_pinned", "is_locked",
            "view_count", "reply_count", "last_activity_at", "created_at",
            "author": { "id", "first_name", "last_name" }
          }, ...
        ]
      }
    Spec ref: §7.2
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    await check_forum_access(forum, current_user)

    qs = Topic.filter(forum=forum)
    total = await qs.count()

    # Pinned topics first, then most recently active
    topics = (
        await qs
        .order_by("-is_pinned", "-last_activity_at")
        .offset((page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("author")
    )

    results = []
    for t in topics:
        last_post = await Post.filter(
            topic=t,
            moderation_status=ModerationStatus.APPROVED,
        ).order_by("-created_at").prefetch_related("author").first()

        last_post_data = None
        if last_post:
            last_post_data = {
                "id":              str(last_post.id),
                "content_preview": last_post.content[:120],
                "created_at":      last_post.created_at,
                "author": {
                    "id":         str(last_post.author_id),
                    "first_name": last_post.author.first_name,
                    "last_name":  last_post.author.last_name,
                },
            }

        results.append({
            "id":               str(t.id),
            "title":            t.title,
            "content":          t.content,
            "attachment":       t.attachment,
            "is_pinned":        t.is_pinned,
            "is_locked":        t.is_locked,
            "view_count":       t.view_count,
            "reply_count":      t.reply_count,
            "last_activity_at": t.last_activity_at,
            "created_at":       t.created_at,
            "author": {
                "id":         str(t.author_id),
                "first_name": t.author.first_name,
                "last_name":  t.author.last_name,
            },
            "last_post": last_post_data,
        })

    return {"total": total, "page": page, "results": results}




@router.get("/topics/pinned", tags=["Forums"])
async def pinned_topic_list(
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view"))
):
    topics = await Topic.filter(is_pinned=True).prefetch_related("forum", "author")

    result = []
    for topic in topics:
        forum = topic.forum  # ✅ No `await` — already prefetched

        perm = await ForumRolePermission.get_or_none(
            forum=forum,
            role=current_user.role,
            can_read=True,  # ✅ Only include topics the user can actually read
        )
        if not perm and not current_user.is_superuser:
            continue

        result.append({
            "id":               str(topic.id),
            "title":            topic.title,
            "content":          topic.content,
            "attachment":       topic.attachment,
            "is_pinned":        topic.is_pinned,
            "is_locked":        topic.is_locked,
            "view_count":       topic.view_count,
            "reply_count":      topic.reply_count,
            "last_activity_at": topic.last_activity_at,
            "created_at":       topic.created_at,
            "author": {
                "id":         str(topic.author_id),
                "first_name": topic.author.first_name,  # ✅ Direct access, already prefetched
                "last_name":  topic.author.last_name,
            },
            "forum": {
                "id":   str(forum.id),
                "name": forum.name,
                "slug": forum.slug,
            },
        })

    return result  # ✅ Always return the list, even if empty


@router.get("/topics/{topic_id}", tags=["Forums"])
async def get_topic(
    topic_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Return a single topic's metadata before loading its posts.
    The UI needs this to render the topic header (title, lock status,
    author, breadcrumb forum name) before the post list loads.

    Response shape:
      {
        "id", "title", "is_pinned", "is_locked",
        "view_count", "reply_count",
        "last_activity_at", "created_at",
        "author": { "id", "first_name", "last_name" },
        "forum":  { "id", "name", "slug" }
      }
    Spec ref: §7.2
    """
    topic = await Topic.get_or_none(id=topic_id).prefetch_related("author")
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")
    forum = await topic.forum
    await check_forum_access(forum, current_user)

    return {
        "id":               str(topic.id),
        "title":            topic.title,
        "content":          topic.content,
        "attachment":       topic.attachment,
        "is_pinned":        topic.is_pinned,
        "is_locked":        topic.is_locked,
        "view_count":       topic.view_count,
        "reply_count":      topic.reply_count,
        "last_activity_at": topic.last_activity_at,
        "created_at":       topic.created_at,
        "author": {
            "id":         str(topic.author_id),
            "first_name": topic.author.first_name,
            "last_name":  topic.author.last_name,
        },
        "forum": {
            "id":   str(forum.id),
            "name": forum.name,
            "slug": forum.slug,
        },
    }





@router.patch("/topics/{topic_id}/pin", tags=["Forums"])
async def pin_topic(
    topic_id:  uuid.UUID,
    pinned:    bool = True,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Pin or unpin a topic (admin/moderator only).
    Pass ?pinned=false to unpin.
    Spec ref: §7.2 (is_pinned field), §15.5
    """
    topic = await Topic.get_or_none(id=topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")
    topic.is_pinned = pinned
    await topic.save()
    return {"id": str(topic.id), "is_pinned": topic.is_pinned}


@router.patch("/topics/{topic_id}/lock", tags=["Forums"])
async def lock_topic(
    topic_id: uuid.UUID,
    locked:   bool = True,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Lock or unlock a topic (admin/moderator only).
    Locked topics reject new posts with 403.
    Pass ?locked=false to reopen.
    Spec ref: §7.2 (is_locked field)
    """
    topic = await Topic.get_or_none(id=topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")
    topic.is_locked = locked
    await topic.save()
    return {"id": str(topic.id), "is_locked": topic.is_locked}


# @router.patch("/topics/{topic_id}/update", tags=["Forums"])
# async def update_topic(
#     topic_id: uuid.UUID,
#     title: str = Form(None),
#     content: str = Form(None),
#     files:   Optional[list[UploadFile]] = File(None),
#     current_user: User = Depends(get_current_user),
# ):
#     """
#     Update a topic's title (author only).
#     Spec ref: §7.2
#     """
#     topic = await Topic.get_or_none(id=topic_id)
#     if not topic:
#         raise HTTPException(status_code=404, detail="Topic not found.")
#     if topic.author_id != current_user.id and current_user.role not in (UserRole.ADMIN, UserRole.MODERATOR):
#         raise HTTPException(status_code=403, detail="Only the topic author or moderators can update the topic.")
#     if title is not None:
#         topic.title = title
#     if content is not None:
#         topic.content = content
#     if files:
#         attachments = []
#         if topic.attachment:
#             # If there are existing attachments, we need to delete them first
#             for att in topic.attachment:
#                 if att:
#                     await delete_file(att)
#         for f in files:
#             file_url = await save_file(file=f, upload_to="topic_attachments")
#             attachments.append(file_url)
#         topic.attachment = attachments if attachments else None
#     await topic.save()
#     await log_activity(current_user, ActivityActionType.TOPIC_UPDATED, "topic", topic.id)
#     return topic



@router.patch("/topics/{topic_id}/update", tags=["Forums"])
async def update_topic(
    topic_id: uuid.UUID,
    title: str = Form(None),
    content: str = Form(None),
    files: Optional[list[UploadFile]] = File(default=None),
    delete_files: Optional[list[str]] = Form(default=None),
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Update a topic's title, content, or attachments (author/moderator/admin only).
    Spec ref: §7.2
    """
    topic = await Topic.get_or_none(id=topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")

    if topic.author_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Only the topic author or moderators can update the topic.")

    if title is not None:
        topic.title = title

    if content is not None:
        topic.content = content


    print(f"Files to update: {files}")

    # Work on a mutable copy of existing attachments
    attachments: list[str] = list(topic.attachment or [])

    # Delete specified attachments
    if delete_files:
        for url in delete_files:
            if url in attachments:
                attachments.remove(url)
                await delete_file(url)  # only delete if it actually belonged to this topic

    
    # Append new uploaded files to existing attachments
    if files:
        for f in files:
            file_url = await save_file(file=f, upload_to="topic_attachments")
            attachments.append(file_url)

    # Persist the final attachment list
    topic.attachment = attachments if attachments else None

    await topic.save()
    await log_activity(current_user, ActivityActionType.TOPIC_UPDATED, "topic", topic.id)
    return topic



@router.delete("/topics/{topic_id}/delete", tags=["Forums"], status_code=204)
async def delete_topic(
    topic_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Delete a topic and all its posts (author or moderator).
    Spec ref: §7.2
    """
    topic = await Topic.get_or_none(id=topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")
    if topic.author_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Only the topic author or moderators can delete the topic.")
    if topic.attachment:
        for att in topic.attachment:
            if att:
                await delete_file(att)
    await log_activity(current_user, ActivityActionType.TOPIC_DELETED, "topic", topic.id)
    await topic.delete()
    return {"status": "Topic deleted successfully."}


@router.post("/forums/{forum_id}/topics", tags=["Forums"], status_code=201)
async def create_topic(
    forum_id: uuid.UUID,
    title: str = Form(...),
    content: str = Form(None),
    files:   list[UploadFile] = File(None),
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Create a new topic in a forum.
    Checks that the user's role has can_post=True on this forum.
    Spec ref: §7.2
    """
    forum = await Forum.get_or_none(id=forum_id)
    if not forum:
        raise HTTPException(status_code=404, detail="Forum not found.")
    await check_forum_access(forum, current_user, need_post=True)
    attachments = []
    if files:
        for f in files:
            file_url = await save_file(file=f, upload_to="topic_attachments")
            attachments.append(file_url)
    topic = await Topic.create(forum=forum, author=current_user, title=title, content=content, attachment=attachments if attachments else None)
    await log_activity(current_user, ActivityActionType.TOPIC_CREATED, "topic", topic.id, title)
    return topic


# ── Posts ───────────────────────────────────────────────────────────────────

@router.get("/topics/{topic_id}/posts", tags=["Forums"])
async def list_posts(
    topic_id:  uuid.UUID,
    page:      int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Paginated list of APPROVED posts in a topic.
    Also increments the topic view count atomically.

    FIX: view_count increment now uses an F-expression to avoid
    the read-modify-write race condition under concurrent requests.

    Response shape:
      {
        "total": int,
        "page":  int,
        "results": [
          {
            "id", "content", "moderation_status",
            "created_at", "updated_at",
            "author": { "id", "first_name", "last_name" }
          }, ...
        ]
      }
    """
    topic = await Topic.get_or_none(id=topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")
    forum = await topic.forum
    await check_forum_access(forum, current_user)

    # FIX: atomic increment — no race condition
    await Topic.filter(id=topic_id).update(view_count=F("view_count") + 1)

    # if current_user.role in (UserRole.ADMIN, UserRole.MODERATOR):
    #     qs = Post.filter(topic=topic)
    # else:
    qs    = Post.filter(topic=topic, moderation_status=ModerationStatus.APPROVED)
    total = await qs.count()
    posts = (
        await qs
        .offset((page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("author")
    )

    results = [
        {
            "id":                str(p.id),
            "content":           p.content,
            "attachment":        p.attachment,
            "moderation_status": p.moderation_status,
            "created_at":        p.created_at,
            "updated_at":        p.updated_at,
            "author": {
                "id":         str(p.author_id),
                "first_name": p.author.first_name,
                "last_name":  p.author.last_name,
            },
        }
        for p in posts
    ]

    return {"total": total, "page": page, "results": results}



@router.patch("/posts/{post_id}", tags=["Forums"])
async def update_post(
    post_id: uuid.UUID,
    content: str = Form(None),
    files:   list[UploadFile] = File(None),
    remove_attachment_urls: Optional[str] = Form(None),
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Update a post's content and attachments (author only).
    Spec ref: §7.3
    """
    post = await Post.get_or_none(id=post_id).prefetch_related("author", "topic")
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.author_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Only the post author or moderators can update the post.")

    if content is not None:
        post.content = content


    current_attachments: List[str] = post.attachment or []
    if remove_attachment_urls:
        import json
        try:
            # Handle both JSON array and plain single URL string
            stripped = remove_attachment_urls.strip()
            if stripped.startswith("["):
                urls_to_remove: List[str] = json.loads(stripped)
            else:
                # Treat as a single URL passed without brackets
                urls_to_remove = [stripped]
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="remove_attachment_urls must be a valid JSON array of strings.")
        for url in urls_to_remove:
            await delete_file(url)
        current_attachments = [u for u in current_attachments if u not in urls_to_remove]

    # Upload and append new attachments
    if files:
        for file in files:
            if file.filename:
                file_url = await save_file(file, upload_to="training_attachments")
                current_attachments.append(file_url)

    post.attachment = current_attachments if current_attachments else None


    await post.save()
    await log_activity(current_user, ActivityActionType.POST_UPDATED, "post", post.id)
    return post



@router.delete("/posts/{post_id}", tags=["Forums"], status_code=204)
async def delete_post(
    post_id: uuid.UUID,
    current_user: User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Delete a post (author or moderator).
    Spec ref: §7.3
    """
    post = await Post.get_or_none(id=post_id).prefetch_related("author", "topic")
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.author_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Only the post author or moderators can delete the post.")
    await log_activity(current_user, ActivityActionType.POST_DELETED, "post", post.id)
    await post.delete()
    return {"status": "Post deleted successfully."}


@router.post("/topics/{topic_id}/posts", tags=["Forums"], status_code=201)
async def create_post(
    topic_id:         uuid.UUID,
    background_tasks: BackgroundTasks,
    content:          str = Form(...),
    files:            list[UploadFile] = File(None),
    current_user:     User = Depends(permission_required(FEATURES.FORUM, "view")),
):
    """
    Submit a new post to a topic.

    - Admin / Moderator posts are immediately APPROVED and visible.
    - All other roles enter PENDING moderation queue.
    - Topic author is notified by email when a reply is approved.
    - Locked topics reject any new post with 403.
    Spec ref: §7.3, §14.1
    """
    topic = await Topic.get_or_none(id=topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found.")
    if topic.is_locked:
        raise HTTPException(status_code=403, detail="This topic is locked.")

    forum = await topic.forum
    await check_forum_access(forum, current_user, need_post=True)

    auto_approve = current_user.is_superuser

    if files:
        attachments = []
        for f in files:
            file_url = await save_file(file=f, upload_to="post_attachments")
            attachments.append(file_url)

    post = await Post.create(
        topic=topic,
        author=current_user,
        content=content,
        attachment=attachments if files else None,
        moderation_status=ModerationStatus.APPROVED if auto_approve else ModerationStatus.PENDING,
        moderated_at=datetime.now(UTC.utc) if auto_approve else None,
        moderated_by=current_user if auto_approve else None,
    )

    if auto_approve:
        await Topic.filter(id=topic_id).update(
            reply_count=F("reply_count") + 1,        # FIX: atomic increment
            last_activity_at=datetime.now(UTC.utc),
        )
        topic_author = await topic.author
        if topic_author.id != current_user.id:
            try:
                await send_email(
                    topic_author, NotificationType.POST_REPLY, "post", post.id, background_tasks
                )
            except Exception as e:
                pass

    await log_activity(current_user, ActivityActionType.POST_CREATED, "post", post.id)
    return {
        "id":                str(post.id),
        "content":           post.content,
        "attachment":        post.attachment,
        "moderation_status": post.moderation_status,
        "created_at":        post.created_at,
        "message": (
            "Post published." if auto_approve
            else "Post submitted and awaiting moderation."
        ),
    }


# ── Moderation ──────────────────────────────────────────────────────────────

@router.get("/moderation/queue", tags=["Moderation"])
async def moderation_queue(
    filter_status: str = Query("all", regex="^(all|pending|flagged)$"),
    page:          int = Query(1, ge=1),
    page_size:     int = Query(20, ge=1, le=100),
    current_user:  User = Depends(permission_required(FEATURES.FORUM, "edit")),
):
    """
    List posts awaiting moderation (pending + flagged).
    Filterable by status. Flagged posts appear first (priority lane).
    Spec ref: §13.1
    """
    if filter_status == "pending":
        qs = Post.filter(moderation_status=ModerationStatus.PENDING)
    elif filter_status == "flagged":
        qs = Post.filter(moderation_status=ModerationStatus.FLAGGED)
    else:
        qs = Post.filter(
            moderation_status__in=[ModerationStatus.PENDING, ModerationStatus.FLAGGED]
        )

    total = await qs.count()
    # Flagged first, then oldest pending first (FIFO queue)
    posts = (
        await qs
        .order_by("-moderation_status", "created_at")
        .offset((page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("author", "topic", "topic__forum")
    )

    results = [
        {
            "id":                str(p.id),
            "content":           p.content,
            "moderation_status": p.moderation_status,
            "created_at":        p.created_at,
            "author": {
                "id":         str(p.author_id),
                "first_name": p.author.first_name,
                "last_name":  p.author.last_name,
            },
            "topic": {
                "id":    str(p.topic_id),
                "title": p.topic.title,
                "forum": p.topic.forum.name,
            },
        }
        for p in posts
    ]

    return {"total": total, "page": page, "results": results}


@router.patch("/posts/{post_id}/moderate", tags=["Moderation"])
async def moderate_post(
    post_id:          uuid.UUID,
    body:             PostModerate,
    background_tasks: BackgroundTasks,
    current_user:     User = Depends(permission_required(FEATURES.FORUM, "edit")),
):
    """
    Approve / Reject / Flag / Forward a post.

    FIXES:
    1. FORWARD action is now handled — assigns moderation to another moderator.
    2. Email notifications are enabled:
       - Rejection always notifies the author (spec §14.1: 'always sent').
       - Approval notifies forum subscribers.
    3. log_activity now correctly branches for all four actions.

    Spec ref: §13.2, §14.1
    """
    post = await Post.get_or_none(id=post_id).prefetch_related("author", "topic")
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")

    post.moderated_by = current_user
    post.moderated_at = datetime.now(UTC.utc)
    log_action_type   = None

    if body.action == ModerationAction.APPROVE:
        post.moderation_status = ModerationStatus.APPROVED
        topic = await post.topic
        # FIX: atomic reply_count increment
        await Topic.filter(id=topic.id).update(
            reply_count=F("reply_count") + 1,
            last_activity_at=datetime.now(UTC.utc),
        )
        post_author = await post.author
        try:
            background_tasks.add_task(_notify_new_post, post.id, post.content)
        except Exception as e:
            print(f"[notify] Failed to enqueue notification task: {e}", flush=True)
        log_action_type = ActivityActionType.POST_APPROVED

    elif body.action == ModerationAction.REJECT:
        if not body.rejection_reason:
            raise HTTPException(
                status_code=422,
                detail="rejection_reason is required when rejecting a post.",
            )
        post.moderation_status = ModerationStatus.REJECTED
        post.rejection_reason  = body.rejection_reason
        author = await post.author
        # FIX: rejection notification is ALWAYS sent per spec §14.1
        # await send_email(
        #     author, NotificationType.POST_REJECTED, "post", post.id, background_tasks
        # )
        print(f"[moderation] Enqueuing rejection email to {author.email} for post {post.id}", flush=True)
        background_tasks.add_task(_notify_post_rejection, author.email)
        log_action_type = ActivityActionType.POST_REJECTED

    elif body.action == ModerationAction.FLAG:
        post.moderation_status = ModerationStatus.FLAGGED
        # FIX: separate log action for flag (not conflated with reject)
        log_action_type = ActivityActionType.POST_FLAGGED   # add this to ActivityActionType enum

    elif body.action == ModerationAction.FORWARD:
        # FIX: FORWARD was previously unhandled
        if not body.forward_to:
            raise HTTPException(
                status_code=422,
                detail="forward_to (moderator UUID) is required for the forward action.",
            )
        target_mod = await User.get_or_none(id=body.forward_to)
        if not target_mod or not target_mod.is_superuser:
            raise HTTPException(
                status_code=400,
                detail="forward_to must reference a valid moderator or admin user.",
            )
        # Post stays PENDING, ownership transferred to the target moderator.
        # assigned_moderator field (added to Post model) tracks who owns the review.
        post.assigned_moderator = target_mod
        post.moderation_status  = ModerationStatus.PENDING
        log_action_type = ActivityActionType.POST_FORWARDED  # add to ActivityActionType enum

    await post.save()

    await ModerationLog.create(
        moderator=current_user,
        post=post,
        action=body.action,
        reason=body.rejection_reason,
    )

    if log_action_type:
        await log_activity(current_user, log_action_type, "post", post.id)

    return {
        "post_id": str(post.id),
        "status":  post.moderation_status,
        "action":  body.action,
    }








# ─── Pydantic schema ───────────────────────────────────────────────────────────

class BulkForumPermissionRequest(BaseModel):
    forum_id: list[uuid.UUID]
    role_id:     list[uuid.UUID]
    can_read: bool
    can_post: bool

    @field_validator("forum_id", "role_id")
    @classmethod
    def no_empty(cls, v):
        if not v:
            raise ValueError("List cannot be empty.")
        return v


# ─── Endpoint ──────────────────────────────────────────────────────────────────

@router.patch("/forums/permissions/bulk", tags=["Forums"])
async def set_forum_permissions_bulk(
    body:         BulkForumPermissionRequest,
    current_user: User = Depends(superuser_required),
):
    # 1. Validate all forum IDs exist in ONE query
    found_forums = await Forum.filter(id__in=body.forum_id, is_active=True).only("id")
    found_ids    = {f.id for f in found_forums}

    missing = set(body.forum_id) - found_ids
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Forums not found or inactive: {[str(m) for m in missing]}",
        )

    # 2. Expand (forum_id x role) combinations
    records = [
        ForumRolePermission(
            forum_id = forum_id,
            role_id     = role_id,
            can_read = body.can_read,
            can_post = body.can_post,
        )
        for forum_id in body.forum_id
        for role_id in body.role_id
    ]

    # 3. Single upsert — one round-trip
    await ForumRolePermission.bulk_create(
        records,
        update_fields=["can_read", "can_post"],
        on_conflict=["forum_id", "role_id"],
    )

    # 4. Return updated rows
    result = await ForumRolePermission.filter(
        forum_id__in=body.forum_id
    ).values("id", "forum_id", "role_id", "can_read", "can_post")

    return {"updated": len(records), "permissions": result}


