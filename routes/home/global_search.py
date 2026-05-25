

from fastapi import APIRouter, Depends, Query
from tortoise.expressions import Q

from app.auth import permission_required
from app.token import get_current_user
from applications.articles.models import Article, ArticleStatus
from applications.forums.models import Forum, ForumRolePermission, Topic
from applications.events.models import Event
from applications.trainings.models import Training
from applications.documents.models import Document, DocumentFolder, DocumentFolderPermission
from applications.user.models import User, Role, UserStatus, FEATURES

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helper: which forum IDs can this user read?
# ─────────────────────────────────────────────────────────────────────────────

async def _readable_forum_ids(user: User | None) -> list:
    """Return UUIDs of forums the user is allowed to read."""
    role = user.role if user else None

    if user.is_superuser:
        return await Forum.filter(is_active=True).values_list("id", flat=True)

    if role is None:
        return []  # Unauthenticated users cannot read forums

    permissions = await ForumRolePermission.filter(role=role, can_read=True).values_list(
        "forum_id", flat=True
    )
    return list(permissions)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: which document folder IDs can this user read?
# ─────────────────────────────────────────────────────────────────────────────

async def _readable_folder_ids(user: User | None) -> list:
    """Return UUIDs of document folders the user is allowed to read."""
    if user is None:
        return []

    role = user.role
    if user.is_superuser:
        return await DocumentFolder.all().values_list("id", flat=True)

    permissions = await DocumentFolderPermission.filter(role=role, can_read=True).values_list(
        "folder_id", flat=True
    )
    return list(permissions)


# ══════════════════════════════════════════════════════════════════════════════
# GET /search/
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/", tags=["Search"])
async def global_search(
    q: str = Query(..., min_length=1, description="Search query string"),
    limit: int = Query(5, ge=1, le=20, description="Max results per category"),
    current_user: User | None = Depends(get_current_user),
):
    """
    Global search across Articles, Forum Topics, Events, Trainings, Documents, and Users.

    - **q**: the search term (required, min 1 character)
    - **limit**: max results returned per category (default 5, max 20)

    Access rules:
    - Articles: unauthenticated users see published only; admins/moderators see all.
    - Topics: filtered by forum read-permissions for the user's role.
    - Events: public events visible to all; non-public only to authenticated users.
    - Trainings: authenticated users only.
    - Documents: filtered by folder read-permissions for the user's role.
    - Users: authenticated users only; admins/moderators see all statuses.
    """

    results = {}
    is_privileged = current_user and current_user.is_superuser

    # ── 1. Articles ────────────────────────────────────────────────────────
    # BUG FIX: prefetch_related() is ignored when using .values() in Tortoise ORM.
    # Use double-underscore traversal inside .values() instead.
    if permission_required(FEATURES.ARTICLE, "view")(current_user):
        article_qs = Article.filter(
            Q(title__icontains=q) | Q(excerpt__icontains=q) | Q(body__icontains=q)
        )

        if not is_privileged:
            article_qs = article_qs.filter(status=ArticleStatus.PUBLISHED)

        articles = (
            await article_qs
            .limit(limit)
            .values(
                "id", "title", "excerpt", "status",
                "published_at", "created_at",
                # structured_fields holds file_urls for thumbnails — needed by article cards
                "structured_fields",
                category_name="category__name",
                author_name="author__first_name",
            )
        )
        results["articles"] = articles

    # ── 2. Forum Topics ────────────────────────────────────────────────────
    readable_forum_ids = await _readable_forum_ids(current_user)
    if permission_required(FEATURES.FORUM, "view")(current_user):
        if readable_forum_ids:
            topics = (
                await Topic.filter(forum_id__in=readable_forum_ids)
                .filter(Q(title__icontains=q) | Q(content__icontains=q))
                .limit(limit)
                .values(
                    "id", "title", "created_at",
                    "reply_count", "view_count",
                    forum_name="forum__name",
                    forum_id="forum_id",
                    author_name="author__first_name",
                )
            )
        else:
            topics = []

        results["topics"] = topics

    # ── 3. Events ──────────────────────────────────────────────────────────
    
    if permission_required(FEATURES.EVENT, "view")(current_user):
        event_qs = Event.filter(
            Q(title__icontains=q) | Q(description__icontains=q) | Q(location__icontains=q)
        )

        if not current_user:
            event_qs = event_qs.filter(is_public=True)

        events = (
            await event_qs
            .limit(limit)
            .values(
                "id", "title", "event_type",
                "event_date", "event_time",   
                "location", "is_public",
            )
        )
        results["events"] = events

    # ── 4. Trainings ───────────────────────────────────────────────────────
    if permission_required(FEATURES.TRAINING, "view")(current_user):
        if current_user:
            trainings = (
                await Training.filter(
                    Q(title__icontains=q) | Q(description__icontains=q)
                )
                .limit(limit)
                .values(
                    "id", "title", "format", "status",
                    "training_date", "duration_hours",
                    # attachments needed to show training thumbnails (like the homepage card)
                    "attachments",
                )
            )
        else:
            trainings = []

        results["trainings"] = trainings


    # ── Summary ────────────────────────────────────────────────────────────
    results["meta"] = {
        "query": q,
        "limit_per_category": limit,
        "totals": {
            "articles":  len(articles),
            "topics":    len(topics),
            "events":    len(events),
            "trainings": len(trainings),
            # "documents": len(documents),
            # "users":     len(users),
        },
    }

    return results