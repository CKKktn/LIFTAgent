"""Bounded raster attachments for collaborative review."""
import base64
import binascii
import hashlib
from io import BytesIO

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def decode_image(upload):
    if upload is None:
        return None, None
    if not isinstance(upload, dict) or not isinstance(upload.get("data"), str):
        raise HTTPException(400, "图片附件格式无效")
    encoded = upload["data"]
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise HTTPException(413, "单张图片最多 8 MB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, "图片数据格式无效") from None
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "单张图片最多 8 MB")
    try:
        with Image.open(BytesIO(content), formats=tuple(FORMATS)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise HTTPException(413, "单张图片最多 2400 万像素，请裁切关注部位后重试")
            if getattr(image, "is_animated", False):
                raise HTTPException(400, "请选择静态 PNG、JPEG 或 WebP 图片")
            metadata = {"media_type": FORMATS[image.format], "width": image.width, "height": image.height}
            image.verify()
        with Image.open(BytesIO(content), formats=tuple(FORMATS)) as image:
            image.load()
    except Image.DecompressionBombError:
        raise HTTPException(413, "单张图片最多 2400 万像素，请裁切关注部位后重试") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise HTTPException(400, "图片无法解码，请使用有效的 PNG、JPEG 或 WebP 文件") from None
    name = upload.get("name")
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip() if isinstance(name, str) else ""
    return {**metadata, "name": name[:240] or "附件图", "sha256": hashlib.sha256(content).hexdigest()}, content


def read_image(db, identifier):
    row = db.one("SELECT media_type,content FROM collab_images WHERE id=?", (identifier,))
    if not row:
        raise HTTPException(404, "会话图片不存在")
    return row
