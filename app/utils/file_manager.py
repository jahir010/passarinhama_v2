import os
import uuid
import aiofiles
import asyncio
from io import BytesIO
from PIL import Image
from fastapi import UploadFile, HTTPException
from app.config import settings






# ------------------------------
# Constants
# ------------------------------
ALLOWED_EXTENSIONS = ["jpg", "jpeg", "png", "gif", "webp", "pdf", "docx", "txt", "mp4", "mp3", "avi", "mkv", "svg",
                      "ai", "eps"]
DEFAULT_MAX_FILE_SIZE_MB = 1024  # 10 MB


# ------------------------------
# Helper Functions
# ------------------------------
def _get_extension(filename: str) -> str:
    return filename.split(".")[-1].lower()


def compress_image_sync(content: bytes, size=(800, 800), quality=50) -> bytes:
    try:
        img = Image.open(BytesIO(content))
        img = img.convert("RGB")
        img.thumbnail(size)
        img_io = BytesIO()
        img.save(img_io, format="WEBP", quality=quality)
        return img_io.getvalue()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image compression failed: {e}")


def _get_folder_path(upload_to: str) -> str:
    folder_path = os.path.join(settings.MEDIA_DIR, upload_to)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


def _get_file_url(relative_path: str) -> str:
    base = settings.BASE_URL.rstrip("/")
    media_root = settings.MEDIA_ROOT.strip("/")
    return f"{base}/{media_root}/{relative_path}"


def _get_relative_path_from_url(file_url: str) -> str | None:
    try:
        base = f"{settings.BASE_URL.rstrip('/')}/{settings.MEDIA_ROOT.strip('/')}/"
        if not file_url.startswith(base):
            return None
        return file_url.replace(base, "")
    except Exception:
        return None


# ------------------------------
# Core Async File Handlers
# ------------------------------
async def save_file(
        file: UploadFile,
        upload_to: str,
        *,
        max_size: int = DEFAULT_MAX_FILE_SIZE_MB,
        allowed_extensions=ALLOWED_EXTENSIONS,
        compress: bool = True,
        quality: int = 50,
        size=(800, 800),
) -> str:
    ext = _get_extension(file.filename)
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Invalid file type: {ext}")

    folder_path = _get_folder_path(upload_to)

    content = bytearray()
    chunk_size = 1024 * 1024  # 1 MB per read
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_size * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File size exceeds the allowed limit")

    if compress and ext in {"jpg", "jpeg", "png", "gif"}:
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(
            None, compress_image_sync, bytes(content), size, quality
        )
        filename = f"{uuid.uuid4().hex}.webp"
    else:
        filename = f"{uuid.uuid4().hex}.{ext}"

    file_path = os.path.join(folder_path, filename)
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    relative_path = f"{upload_to}/{filename}"
    return _get_file_url(relative_path)


async def delete_file(file_url: str) -> bool:
    if not file_url:
        return False

    relative_path = _get_relative_path_from_url(file_url)
    if not relative_path:
        return False

    abs_path = os.path.join(settings.MEDIA_DIR, relative_path)
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
            return True
        except Exception as e:
            print(f"⚠️ Failed to delete file {abs_path}: {e}")
    return False


async def update_file(
        new_file: UploadFile,
        file_url: str | None,
        upload_to: str,
        *,
        max_size: int = DEFAULT_MAX_FILE_SIZE_MB,
        allowed_extensions=ALLOWED_EXTENSIONS,
        compress: bool = True,
        quality: int = 50,
        size=(800, 800),
) -> str:
    if file_url:
        await delete_file(file_url)

    return await save_file(
        new_file,
        upload_to=upload_to,
        max_size=max_size,
        allowed_extensions=allowed_extensions,
        compress=compress,
        quality=quality,
        size=size,
    )






# import asyncio
# from io import BytesIO
# from PIL import Image
# from fastapi import UploadFile, HTTPException
# import cloudinary
# import cloudinary.uploader
# import cloudinary.api
# from app.config import settings

# # ------------------------------
# # Cloudinary Configuration
# # ------------------------------
# cloudinary.config(
#     cloud_name="dpnsbsqsi",
#     api_key="841763193165159",
#     api_secret="yGelBYRkMhtENOxQRtbPHiTBN7s"
# )

# # ------------------------------
# # Constants
# # ------------------------------
# ALLOWED_EXTENSIONS = [
#     "jpg", "jpeg", "png", "gif", "webp",
#     "pdf", "docx", "txt",
#     "mp4", "mp3", "avi", "mkv",
#     "svg", "ai", "eps"
# ]
# DEFAULT_MAX_FILE_SIZE_MB = 1024  # 1 GB

# IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
# VIDEO_EXTENSIONS = {"mp4", "avi", "mkv"}
# AUDIO_EXTENSIONS = {"mp3"}
# RAW_EXTENSIONS   = {"pdf", "docx", "txt", "svg", "ai", "eps"}


# # ------------------------------
# # Helper Functions
# # ------------------------------
# def _get_extension(filename: str) -> str:
#     return filename.rsplit(".", 1)[-1].lower()


# def _get_resource_type(ext: str) -> str:
#     """Map file extension to Cloudinary resource_type."""
#     if ext in IMAGE_EXTENSIONS:
#         return "image"
#     if ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS:
#         return "video"
#     return "raw"


# def _extract_public_id(secure_url: str, resource_type: str) -> str | None:
#     """
#     Extract the Cloudinary public_id (including folder) from a secure URL.
#     Example URL:
#       https://res.cloudinary.com/<cloud>/image/upload/v123/uploads/products/abc123.webp
#     Returns: uploads/products/abc123
#     """
#     try:
#         marker = f"/{resource_type}/upload/"
#         idx = secure_url.find(marker)
#         if idx == -1:
#             return None
#         after = secure_url[idx + len(marker):]
#         # Strip version segment like "v1234567890/"
#         if after.startswith("v") and "/" in after:
#             parts = after.split("/", 1)
#             if parts[0][1:].isdigit():
#                 after = parts[1]
#         # Remove file extension
#         public_id = after.rsplit(".", 1)[0]
#         return public_id
#     except Exception:
#         return None


# def compress_image_sync(content: bytes, size=(800, 800), quality=50) -> bytes:
#     try:
#         img = Image.open(BytesIO(content))
#         img = img.convert("RGB")
#         img.thumbnail(size)
#         img_io = BytesIO()
#         img.save(img_io, format="WEBP", quality=quality)
#         return img_io.getvalue()
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Image compression failed: {e}")


# # ------------------------------
# # Core Async File Handlers
# # ------------------------------
# async def save_file(
#         file: UploadFile,
#         upload_to: str,
#         *,
#         max_size: int = DEFAULT_MAX_FILE_SIZE_MB,
#         allowed_extensions=ALLOWED_EXTENSIONS,
#         compress: bool = True,
#         quality: int = 50,
#         size=(800, 800),
# ) -> str:
#     """
#     Upload a file to Cloudinary and return its secure URL.

#     Args:
#         file:               FastAPI UploadFile object.
#         upload_to:          Cloudinary folder path (e.g. "products/images").
#         max_size:           Maximum allowed file size in MB.
#         allowed_extensions: Whitelist of permitted file extensions.
#         compress:           Whether to compress images before uploading.
#         quality:            WEBP quality (1-100) when compress=True.
#         size:               Max (width, height) thumbnail size when compress=True.

#     Returns:
#         Cloudinary secure URL string.
#     """
#     ext = _get_extension(file.filename)
#     if ext not in allowed_extensions:
#         raise HTTPException(status_code=400, detail=f"Invalid file type: .{ext}")

#     # --- Stream & size-check ---
#     content = bytearray()
#     chunk_size = 1024 * 1024  # 1 MB
#     while True:
#         chunk = await file.read(chunk_size)
#         if not chunk:
#             break
#         content.extend(chunk)
#         if len(content) > max_size * 1024 * 1024:
#             raise HTTPException(status_code=400, detail="File size exceeds the allowed limit")

#     content = bytes(content)

#     # --- Optional image compression (convert to WEBP) ---
#     upload_format = None
#     if compress and ext in IMAGE_EXTENSIONS - {"webp"}:
#         loop = asyncio.get_running_loop()
#         content = await loop.run_in_executor(
#             None, compress_image_sync, content, size, quality
#         )
#         upload_format = "webp"

#     resource_type = _get_resource_type(upload_format or ext)

#     # --- Upload to Cloudinary ---
#     loop = asyncio.get_running_loop()
#     try:
#         result = await loop.run_in_executor(
#             None,
#             lambda: cloudinary.uploader.upload(
#                 BytesIO(content),
#                 folder=upload_to,
#                 resource_type=resource_type,
#                 **({"format": upload_format} if upload_format else {}),
#             ),
#         )
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {e}")

#     return result["secure_url"]


# async def delete_file(file_url: str) -> bool:
#     """
#     Delete a file from Cloudinary by its secure URL.

#     Returns True if deleted, False otherwise.
#     """
#     if not file_url:
#         return False

#     # Determine resource type from the URL itself
#     for rtype in ("image", "video", "raw"):
#         if f"/{rtype}/upload/" in file_url:
#             resource_type = rtype
#             break
#     else:
#         return False

#     public_id = _extract_public_id(file_url, resource_type)
#     if not public_id:
#         return False

#     loop = asyncio.get_running_loop()
#     try:
#         result = await loop.run_in_executor(
#             None,
#             lambda: cloudinary.uploader.destroy(
#                 public_id,
#                 resource_type=resource_type,
#                 invalidate=True,
#             ),
#         )
#         return result.get("result") == "ok"
#     except Exception as e:
#         print(f"⚠️ Cloudinary delete failed for '{public_id}': {e}")
#         return False


# async def update_file(
#         new_file: UploadFile,
#         file_url: str | None,
#         upload_to: str,
#         *,
#         max_size: int = DEFAULT_MAX_FILE_SIZE_MB,
#         allowed_extensions=ALLOWED_EXTENSIONS,
#         compress: bool = True,
#         quality: int = 50,
#         size=(800, 800),
# ) -> str:
#     """
#     Replace an existing Cloudinary file with a new one.

#     Deletes the old file (if a URL is provided) and uploads the new file.
#     Returns the new file's secure URL.
#     """
#     if file_url:
#         await delete_file(file_url)

#     return await save_file(
#         new_file,
#         upload_to=upload_to,
#         max_size=max_size,
#         allowed_extensions=allowed_extensions,
#         compress=compress,
#         quality=quality,
#         size=size,
#     )