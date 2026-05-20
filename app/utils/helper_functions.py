from applications.forums.models import Forum, ForumRolePermission
from applications.user.models import User, ActivityActionType, ActivityLog
from applications.documents.models import DocumentFolder, DocumentFolderPermission
# from applications.trainings.models import TrainingRolePermission, Training
from fastapi import HTTPException
import uuid




async def check_forum_access(forum: Forum, user: User, need_post: bool = False) -> None:
    if user.is_superuser:
        return  # superusers bypass all permission checks
    perm = await ForumRolePermission.get_or_none(forum=forum, role=user.role)
    if perm is None or not perm.can_read:
        raise HTTPException(status_code=403, detail="You do not have access to this forum.")
    if need_post and not perm.can_post:
        raise HTTPException(status_code=403, detail="Your role does not allow posting in this forum.")

async def check_folder_access(folder: DocumentFolder, user: User, need_upload: bool = False) -> None:
    if user.is_superuser:
        return  # superusers bypass all permission checks
    perm = await DocumentFolderPermission.get_or_none(folder=folder, role=user.role)
    if perm is None or not perm.can_read:
        raise HTTPException(status_code=403, detail="You do not have access to this folder.")
    if need_upload and not perm.can_upload:
        raise HTTPException(status_code=403, detail="Your role does not allow uploading to this folder.")
    
# async def check_training_access(training: Training, user: User, need_post: bool = False) -> None:
#     if user.is_superuser:
#         return  # superusers bypass all permission checks
#     perm = await TrainingRolePermission.get_or_none(training=training, role=user.role)
#     if perm is None or not perm.can_read:
#         raise HTTPException(status_code=403, detail="You do not have access to this content.")
#     if need_post and not perm.can_post:
#         raise HTTPException(status_code=403, detail="Your role does not allow to chnage this section.")

async def log_activity(user: User, action: ActivityActionType, target_type: str | None = None,
                       target_id: uuid.UUID | None = None, description: str | None = None) -> None:
    await ActivityLog.create(
        user=user, action_type=action,
        target_type=target_type, target_id=target_id, description=description,
    )