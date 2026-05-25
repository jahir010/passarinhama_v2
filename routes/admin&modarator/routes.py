
from fastapi import APIRouter, Depends, HTTPException, Query
from tortoise.expressions import F
from pydantic import BaseModel
from datetime import date, timezone as UTC

from app.auth import superuser_required, permission_required
from app.token import get_current_user

from applications.user.models import ActivityLog, User, UserStatus, FEATURES
from applications.forums.models import Forum, ForumRolePermission, Topic, Post, ModerationStatus, ModerationLog
from applications.events.models import Event
from applications.trainings.models import Training, TrainingStatus, TrainingRegistration
from applications.articles.models import Article, ArticleStatus


router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────────────────────────────────────

class DashboardStats(BaseModel):
    total_active_members: int
    active_topics:        int
    events_this_year:     int
    trainings_planned:    int


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD — 
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard/stats", response_model=DashboardStats, tags=["Dashboard"])
async def dashboard_stats(current_user: User = Depends(get_current_user)):
    """
    Stat cards (top row):
      - Total active members
      - Active forum topics count
      - Events scheduled this year
      - Trainings planned
    """
    today      = date.today()
    year_start = date(today.year, 1, 1)
    year_end   = date(today.year + 1, 1, 1)

    return DashboardStats(
        total_active_members=await User.filter(
            status=UserStatus.ACTIVE,
            is_payment_validated=True,
            is_deleted=False,
        ).count(),
        active_topics=await Topic.filter(is_locked=False).count(),
        events_this_year=await Event.filter(
            event_date__gte=year_start,
            event_date__lt=year_end,
        ).count(),
        trainings_planned=await Training.filter(
            status__in=[TrainingStatus.OPEN, TrainingStatus.FULL],
        ).count(),
    )


@router.get("/dashboard/activity", tags=["Dashboard"])
async def dashboard_activity(current_user: User = Depends(get_current_user)):
    logs = (
        await ActivityLog.all()
        .order_by("-created_at")
        .limit(10)
        .prefetch_related("user")
    )

    badge_map = {
        "post_created":        "info",
        "post_approved":       "success",
        "post_rejected":       "danger",
        "post_flagged":        "warning",
        "topic_created":       "info",
        "article_published":   "success",
        "member_registered":   "info",
        "training_registered": "info",
        "document_uploaded":   "secondary",
    }

    return [
        {
            "id":          str(log.id),
            "action_type": log.action_type,
            "target_type": log.target_type,
            "target_id":   str(log.target_id) if log.target_id else None,
            "description": log.description,
            "created_at":  log.created_at.isoformat(),
            "status_badge": badge_map.get(log.action_type, "secondary"), 
            "actor": {
                "id":         str(log.user_id),
                "first_name": log.user.first_name,
                "last_name":  log.user.last_name,
            },
        }
        for log in logs
    ]


@router.get("/dashboard/upcoming-events", tags=["Dashboard"])
async def dashboard_upcoming_events(current_user: User = Depends(permission_required(FEATURES.EVENT, "view"))):
   
    today  = date.today()
    events = (
        await Event.filter(event_date__gte=today)
        .order_by("event_date", "event_time")
        .limit(6)
    )
    return [
        {
            "id":         str(e.id),
            "title":      e.title,
            "event_type": e.event_type,
            "day":        e.event_date.day,
            "month":      e.event_date.strftime("%b"),   # "May", "Jun" …
            "location":   e.location,
            "is_public":  e.is_public,
        }
        for e in events
    ]


@router.get("/dashboard/latest-articles", tags=["Dashboard"])
async def dashboard_latest_articles(current_user: User = Depends(permission_required(FEATURES.ARTICLE, "view"))):
    articles = (
        await Article.filter(status=ArticleStatus.PUBLISHED)
        .order_by("-published_at")
        .limit(4)
        .prefetch_related("category", "author")
    )
    return [
        {
            "id":           str(a.id),
            "title":        a.title,
            "excerpt":      a.excerpt if a.excerpt else None,  # short preview
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "category": {
                "id":   str(a.category_id) if a.category_id else None,
                "name": a.category.name if a.category_id else None,
            },
            "author": {
                "id":         str(a.author_id),
                "first_name": a.author.first_name,
                "last_name":  a.author.last_name,
            },
        }
        for a in articles
    ]


@router.get("/dashboard/latest-posts", tags=["Dashboard"])
async def dashboard_latest_posts(current_user: User = Depends(permission_required(FEATURES.FORUM, "view"))):
    # Get forums this user can read
    readable_perms = await ForumRolePermission.filter(
        role=current_user.role, can_read=True
    ).values_list("forum_id", flat=True)

    # Get the 4 most recent approved posts in those forums
    posts = (
        await Post.filter(
            moderation_status=ModerationStatus.APPROVED,
            topic__forum_id__in=readable_perms,
        )
        .order_by("-created_at")
        .limit(4)
        .prefetch_related("author", "topic", "topic__forum")
    )

    return [
        {
            "id":         str(p.id),
            "content_preview": p.content[:150],
            "created_at": p.created_at.isoformat(),
            "author": {
                "id":         str(p.author_id),
                "first_name": p.author.first_name,
                "last_name":  p.author.last_name,
            },
            "topic": {
                "id":    str(p.topic_id),
                "title": p.topic.title,
            },
            "forum": {
                "id":   str(p.topic.forum_id),
                "name": p.topic.forum.name,
            },
        }
        for p in posts
    ]


@router.get("/dashboard/upcoming-trainings", tags=["Dashboard"])
async def dashboard_upcoming_trainings(current_user: User = Depends(permission_required(FEATURES.TRAINING, "view"))):
    today     = date.today()
    trainings = (
        await Training.filter(training_date__gte=today)
        .order_by("training_date")
        .limit(4)
    )

    result = []
    for t in trainings:
        attendee_count = await TrainingRegistration.filter(training=t).count()
        spots_left = (
            max(0, t.max_attendees - attendee_count)
            if t.max_attendees is not None else None
        )
        is_registered = await TrainingRegistration.filter(
            training=t, user=current_user
        ).exists()

        result.append({
            "id":             str(t.id),
            "title":          t.title,
            "format":         t.format,
            "description":    t.description,
            "training_date":  t.training_date.isoformat() if t.training_date else None,
            "end_date":       t.end_date.isoformat() if t.end_date else None,
            "duration_hours": t.duration_hours,
            "max_attendees":  t.max_attendees,
            "thumbnail_url":  t.thumbnail_url,
            "attendee_count": attendee_count,
            "spots_left":     spots_left,
            "is_at_capacity": spots_left == 0 if spots_left is not None else False,
            "is_registered":  is_registered,
            "status":         t.status,
        })

    return result


# ══════════════════════════════════════════════════════════════════════════════
# MODERATION — §13
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/moderation/queue", tags=["Moderation"])
async def moderation_queue(
    filter_type: str | None = Query(None, regex="^(pending|flagged)$"),
    page:        int        = Query(1, ge=1),
    page_size:   int        = Query(20, ge=1, le=100),
    current_user: User      = Depends(permission_required(FEATURES.FORUM, "edit"))
):
    if filter_type == "flagged":
        qs = Post.filter(moderation_status=ModerationStatus.FLAGGED)
    elif filter_type == "pending":
        qs = Post.filter(moderation_status=ModerationStatus.PENDING)
    else:
        qs = Post.filter(
            moderation_status__in=[ModerationStatus.PENDING, ModerationStatus.FLAGGED]
        )

    total = await qs.count()
    posts = (
        await qs
        .order_by("-moderation_status", "created_at")   # flagged first, then oldest pending
        .offset((page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("author", "topic", "topic__forum")
    )

    return {
        "total": total,
        "page":  page,
        "results": [
            {
                "id":                str(p.id),
                "content":           p.content,
                "moderation_status": p.moderation_status,
                "created_at":        p.created_at.isoformat(),
                "author": {
                    "id":         str(p.author_id),
                    "first_name": p.author.first_name,
                    "last_name":  p.author.last_name,
                },
                "topic": {
                    "id":    str(p.topic_id),
                    "title": p.topic.title,
                },
                "forum": {
                    "id":   str(p.topic.forum_id),
                    "name": p.topic.forum.name,
                },
            }
            for p in posts
        ],
    }


@router.get("/moderation/logs", tags=["Moderation"])
async def moderation_logs(
    page:      int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(permission_required(FEATURES.FORUM, "edit")),
):
    total = await ModerationLog.all().count()
    logs  = (
        await ModerationLog.all()
        .order_by("-acted_at")
        .offset((page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("moderator", "post", "post__author")
    )
    return {
        "total": total,
        "page":  page,
        "results": [
            {
                "id":       str(log.id),
                "action":   log.action,
                "reason":   log.reason,
                "acted_at": log.acted_at.isoformat(),
                "moderator": {
                    "id":         str(log.moderator_id),
                    "first_name": log.moderator.first_name,
                    "last_name":  log.moderator.last_name,
                },
                "post": {
                    "id":      str(log.post_id),
                    "content": log.post.content[:100],
                    "author": {
                        "id":         str(log.post.author_id),
                        "first_name": log.post.author.first_name,
                        "last_name":  log.post.author.last_name,
                    },
                },
            }
            for log in logs
        ],
    }





# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — ACTIVITY LOG 
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/activity-log", tags=["Admin"])
async def activity_log(
    page:      int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(superuser_required)
):
    
    total = await ActivityLog.all().count()
    logs  = (
        await ActivityLog.all()
        .order_by("-created_at")
        .offset((page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("user")
    )
    return {
        "total": total,
        "page":  page,
        "results": [
            {
                "id":          str(log.id),
                "action_type": log.action_type,
                "target_type": log.target_type,
                "target_id":   str(log.target_id) if log.target_id else None,
                "description": log.description,
                "created_at":  log.created_at.isoformat(),
                "actor": {
                    "id":         str(log.user_id),
                    "first_name": log.user.first_name,
                    "last_name":  log.user.last_name,
                },
            }
            for log in logs
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC — no auth required 
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/public/articles", tags=["Public"])
async def public_articles():
    
    articles = (
        await Article.filter(status=ArticleStatus.PUBLISHED)
        .order_by("-published_at")
        .limit(4)
        .prefetch_related("category", "author")
    )
    return [
        {
            "id":           str(a.id),
            "title":        a.title,
            "excerpt":      a.excerpt if a.excerpt else a.body[:200] if a.body else None,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "category": {
                "id":   str(a.category_id) if a.category_id else None,
                "name": a.category.name if a.category_id else None,
            },
        }
        for a in articles
    ]


@router.get("/public/events", tags=["Public"])
async def public_events():
    
    today  = date.today()
    events = (
        await Event.filter(is_public=True, event_date__gte=today)
        .order_by("event_date")
        .limit(6)
    )
    return [
        {
            "id":         str(e.id),
            "title":      e.title,
            "event_type": e.event_type,
            "day":        e.event_date.day,
            "month":      e.event_date.strftime("%b"),
            "location":   e.location,
        }
        for e in events
    ]