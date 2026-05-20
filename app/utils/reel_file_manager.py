import asyncio
from typing import Optional, Set, Tuple

from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.utils.file_manager import save_file, update_file
from applications.reels.reels import Reel

VIDEO_EXTENSIONS: Set[str] = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
IMAGE_EXTENSIONS: Set[str] = {"jpg", "jpeg", "png", "webp", "svg", "gif"}
MAX_VIDEO_SIZE_MB = 300
MAX_IMAGE_SIZE_MB = 20


def _has_upload(file: Optional[UploadFile]) -> bool:
    return bool(file and file.filename and file.filename.strip())


def _validate_extension(file: UploadFile, allowed_extensions: Set[str], label: str) -> None:
    filename = (file.filename or "").strip()
    if "." not in filename:
        raise HTTPException(status_code=400, detail=f"{label} has no valid extension")

    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Invalid {label} type: {extension}")


def _validate_size_hint(file: UploadFile, max_size_mb: int, label: str) -> None:
    size = getattr(file, "size", None)
    if isinstance(size, int) and size > max_size_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"{label} exceeds {max_size_mb}MB size limit")


def validate_reel_upload_inputs(
    *,
    media_file: Optional[UploadFile] = None,
    thumbnail: Optional[UploadFile] = None,
    logo: Optional[UploadFile] = None,
) -> None:
    if _has_upload(media_file):
        _validate_extension(media_file, VIDEO_EXTENSIONS, "media_file")
        _validate_size_hint(media_file, MAX_VIDEO_SIZE_MB, "media_file")
    if _has_upload(thumbnail):
        _validate_extension(thumbnail, IMAGE_EXTENSIONS, "thumbnail")
        _validate_size_hint(thumbnail, MAX_IMAGE_SIZE_MB, "thumbnail")
    if _has_upload(logo):
        _validate_extension(logo, IMAGE_EXTENSIONS, "logo")
        _validate_size_hint(logo, MAX_IMAGE_SIZE_MB, "logo")


async def _save_or_update_uploaded_file(
    file: UploadFile,
    *,
    existing_url: Optional[str],
    upload_to: str,
    max_size: int,
    allowed_extensions: Set[str],
    compress: bool,
) -> str:
    if existing_url:
        return await update_file(
            file,
            existing_url,
            upload_to=upload_to,
            max_size=max_size,
            allowed_extensions=allowed_extensions,
            compress=compress,
        )
    return await save_file(
        file,
        upload_to=upload_to,
        max_size=max_size,
        allowed_extensions=allowed_extensions,
        compress=compress,
    )


async def _upload_single_field(
    field_name: str,
    file: UploadFile,
    *,
    existing_url: Optional[str],
    upload_to: str,
    max_size: int,
    allowed_extensions: Set[str],
    compress: bool,
) -> Tuple[str, Optional[str], Optional[str]]:
    try:
        await file.seek(0)
        uploaded_url = await _save_or_update_uploaded_file(
            file,
            existing_url=existing_url,
            upload_to=upload_to,
            max_size=max_size,
            allowed_extensions=allowed_extensions,
            compress=compress,
        )
        return field_name, uploaded_url, None
    except Exception as error:
        return field_name, None, str(error)


async def process_reel_uploads_in_background(
    reel_id: int,
    media_file: Optional[UploadFile] = None,
    thumbnail: Optional[UploadFile] = None,
    logo: Optional[UploadFile] = None,
    old_media_url: Optional[str] = None,
    old_thumbnail_url: Optional[str] = None,
    old_logo_url: Optional[str] = None,
) -> None:
    try:
        upload_jobs = []

        if _has_upload(media_file):
            assert media_file is not None
            upload_jobs.append(
                _upload_single_field(
                    "media_file",
                    media_file,
                    existing_url=old_media_url,
                    upload_to="reels/videos",
                    max_size=MAX_VIDEO_SIZE_MB,
                    allowed_extensions=VIDEO_EXTENSIONS,
                    compress=False,
                )
            )

        if _has_upload(thumbnail):
            assert thumbnail is not None
            upload_jobs.append(
                _upload_single_field(
                    "thumbnail",
                    thumbnail,
                    existing_url=old_thumbnail_url,
                    upload_to="reels/thumbnails",
                    max_size=MAX_IMAGE_SIZE_MB,
                    allowed_extensions=IMAGE_EXTENSIONS,
                    compress=True,
                )
            )

        if _has_upload(logo):
            assert logo is not None
            upload_jobs.append(
                _upload_single_field(
                    "logo",
                    logo,
                    existing_url=old_logo_url,
                    upload_to="reels/logos",
                    max_size=MAX_IMAGE_SIZE_MB,
                    allowed_extensions=IMAGE_EXTENSIONS,
                    compress=True,
                )
            )

        if not upload_jobs:
            return

        results = await asyncio.gather(*upload_jobs)
        update_data = {}
        failed_fields = {}
        for field, uploaded_url, error in results:
            if uploaded_url:
                update_data[field] = uploaded_url
            if error:
                failed_fields[field] = error

        if update_data:
            updated_rows = await Reel.filter(id=reel_id).update(**update_data)
            if not updated_rows:
                print(f"[reel-upload] skipped db update for missing reel_id={reel_id}")

        if failed_fields:
            print(f"[reel-upload] partial failure for reel_id={reel_id}: {failed_fields}")
    except Exception as error:
        print(f"[reel-upload] failed for reel_id={reel_id}: {error}")
    finally:
        for file in (media_file, thumbnail, logo):
            if file:
                try:
                    await file.close()
                except Exception:
                    pass


def queue_reel_upload_task(
    background_tasks: BackgroundTasks,
    *,
    reel_id: int,
    media_file: Optional[UploadFile] = None,
    thumbnail: Optional[UploadFile] = None,
    logo: Optional[UploadFile] = None,
    old_media_url: Optional[str] = None,
    old_thumbnail_url: Optional[str] = None,
    old_logo_url: Optional[str] = None,
) -> str:
    validate_reel_upload_inputs(media_file=media_file, thumbnail=thumbnail, logo=logo)

    if not any((_has_upload(media_file), _has_upload(thumbnail), _has_upload(logo))):
        return "not_provided"

    background_tasks.add_task(
        process_reel_uploads_in_background,
        reel_id=reel_id,
        media_file=media_file,
        thumbnail=thumbnail,
        logo=logo,
        old_media_url=old_media_url,
        old_thumbnail_url=old_thumbnail_url,
        old_logo_url=old_logo_url,
    )
    return "processing"
