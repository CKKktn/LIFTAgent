from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
import hashlib
import logging
from math import isfinite
from pathlib import Path
import posixpath
from threading import Lock
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image, ImageOps

DRAWING_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
OFFICE_IMAGE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})
DRAWING_EXTENSIONS = DRAWING_IMAGE_EXTENSIONS | OFFICE_IMAGE_EXTENSIONS | {".pdf"}

_MAX_PAGE_EDGE = 3072
_OVERVIEW_EDGE = 1280
_DETAIL_EDGE = 1792
_PDF_SCALE = 3
_PDF_LOCK = Lock()
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DrawingPage:
    number: int
    image: Image.Image
    source_part: str = ""
    slide_number: int | None = None


@dataclass(frozen=True)
class VisionRegion:
    id: str
    box: tuple[float, float, float, float]
    image: Image.Image


def drawing_pages(path: Path, *, text_pages: set[int], force_pdf: bool = False,
                  source_part: str = "", warnings: list[str] | None = None) -> Iterator[DrawingPage]:
    """Yield drawing candidates as displayed, or all nonblank PDF pages when forced."""
    extension = path.suffix.lower()
    if extension == ".pdf":
        yield from _pdf_pages(path, text_pages, force_pdf=force_pdf)
    elif extension in OFFICE_IMAGE_EXTENSIONS:
        yield from _office_images(path, source_part=source_part, warnings=warnings)
    elif extension in DRAWING_IMAGE_EXTENSIONS:
        try:
            with Image.open(path, formats=("PNG", "JPEG")) as source, ImageOps.exif_transpose(source) as oriented:
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    with oriented.convert("RGBA") as rgba:
                        rgba.thumbnail((_MAX_PAGE_EDGE, _MAX_PAGE_EDGE), Image.Resampling.LANCZOS)
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    oriented.thumbnail((_MAX_PAGE_EDGE, _MAX_PAGE_EDGE), Image.Resampling.LANCZOS)
                    image = oriented.convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError):
            _LOGGER.debug("Skipping unreadable drawing image")
            return
        yield DrawingPage(1, image)


def _office_images(path: Path, *, source_part: str, warnings: list[str] | None) -> Iterator[DrawingPage]:
    """Read embedded image bytes without following external Office relationships.

    The image number is a package image index, never a Word/PDF page number.
    Native slide shapes require a slide renderer, so report that coverage limit.
    """
    media_root = {".docx": "word/media/", ".pptx": "ppt/media/", ".xlsx": "xl/media/"}[path.suffix.lower()]
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 20000 or sum(item.file_size for item in infos) > 200 * 1024 * 1024:
            raise ValueError("Office 文件展开后过大，请拆分后上传")
        slides = {}
        if path.suffix.lower() == ".pptx":
            for name in archive.namelist():
                if name.startswith("ppt/slides/_rels/slide") and name.endswith(".xml.rels"):
                    number = int(name.rsplit("slide", 1)[1].split(".", 1)[0])
                    for relation in ET.fromstring(archive.read(name)):
                        if relation.get("Type", "").endswith("/image") and relation.get("TargetMode") != "External":
                            target = posixpath.normpath(posixpath.join("ppt/slides", relation.get("Target", "")))
                            slides.setdefault(target, number)
            if warnings is not None:
                warnings.append("PPT 已提取原生文字和内嵌图片；原生线条、形状及其空间组合请结合整张幻灯片核对，或另存为 PDF 上传。")
        seen = set()
        unreadable = []
        index = 0
        for info in infos:
            if not info.filename.startswith(media_root) or info.is_dir():
                continue
            if source_part and source_part != info.filename:
                continue
            data = archive.read(info)
            digest = hashlib.sha256(data).digest()
            if digest in seen:
                continue
            seen.add(digest)
            try:
                with Image.open(BytesIO(data)) as original, ImageOps.exif_transpose(original) as oriented:
                    if min(oriented.size) < 24:
                        continue
                    oriented.thumbnail((_MAX_PAGE_EDGE, _MAX_PAGE_EDGE), Image.Resampling.LANCZOS)
                    with oriented.convert("RGBA") as rgba:
                        image = Image.new("RGB", rgba.size, "white")
                        image.paste(rgba, mask=rgba.getchannel("A"))
            except (OSError, ValueError, Image.DecompressionBombError):
                unreadable.append(info.filename)
                continue
            index += 1
            yield DrawingPage(index, image, info.filename, slides.get(info.filename))
        if unreadable and warnings is not None:
            warnings.append(f"有 {len(unreadable)} 个内嵌图像未能解码，例如 {unreadable[0]}；请另存为 PNG 或 PDF 后补充。")


def _pdf_pages(path: Path, text_pages: set[int], *, force_pdf: bool) -> Iterator[DrawingPage]:
    import pypdfium2 as pdfium

    # PDFium requires serialization even when threads open different documents.
    # Release the lock while the caller processes each detached PIL image.
    try:
        with _PDF_LOCK:
            document = pdfium.PdfDocument(path)
    except (pdfium.PdfiumError, OSError, ValueError):
        _LOGGER.debug("Skipping unreadable or password-protected drawing PDF")
        return
    try:
        with _PDF_LOCK:
            page_count = len(document)
        for index in range(page_count):
            number = index + 1
            try:
                with _PDF_LOCK, closing(document[index]) as page:
                    image = _render_drawing(page, has_extracted_text=number in text_pages, force_pdf=force_pdf)
            except (pdfium.PdfiumError, OSError, ValueError):
                _LOGGER.debug("Skipping unreadable drawing PDF page %d", number)
                continue
            if image is not None:
                yield DrawingPage(number, image)
    finally:
        with _PDF_LOCK:
            document.close()


def _render_drawing(page, *, has_extracted_text: bool, force_pdf: bool) -> Image.Image | None:
    width, height = page.get_size()
    if not all(isfinite(edge) and edge > 0 for edge in (width, height)):
        return None
    if not force_pdf and not _has_drawing_objects(page, has_extracted_text):
        return None
    scale = min(_PDF_SCALE, _MAX_PAGE_EDGE / max(width, height))
    with closing(page.render(scale=scale)) as bitmap, bitmap.to_pil() as pixels:
        image = pixels.convert("RGB")
    image.thumbnail((_MAX_PAGE_EDGE, _MAX_PAGE_EDGE), Image.Resampling.LANCZOS)
    if all(low >= 248 for low, _high in image.getextrema()):
        image.close()
        return None
    return image


def _has_drawing_objects(page, has_extracted_text: bool) -> bool:
    from pypdfium2 import raw

    left, bottom, right, top = page.get_bbox()
    width, height = right - left, top - bottom
    if width <= 0 or height <= 0:
        return False
    has_text = has_extracted_text
    has_graphics = False
    horizontal_strokes = vertical_strokes = 0
    has_long_horizontal = has_long_vertical = False
    kinds = [raw.FPDF_PAGEOBJ_TEXT, raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING]
    for item in page.get_objects(filter=kinds):
        if item.type == raw.FPDF_PAGEOBJ_TEXT:
            has_text = True
            continue
        if item.type == raw.FPDF_PAGEOBJ_SHADING:
            has_graphics = True
            continue
        has_graphics = True
        # Nested Form XObjects use their own coordinate system. Keep their
        # graphics as candidates and let rendering remove empty output.
        if item.level:
            return True
        x0, y0, x1, y1 = item.get_bounds()
        span_x = max(0, min(x1, right) - max(x0, left)) / width
        span_y = max(0, min(y1, top) - max(y0, bottom)) / height
        if item.type == raw.FPDF_PAGEOBJ_IMAGE:
            if min(span_x, span_y) >= 0.03 and span_x * span_y >= 0.008:
                return True
        elif item.type == raw.FPDF_PAGEOBJ_PATH:
            if min(span_x, span_y) >= 0.04:
                return True
            if span_x >= max(0.01, 2 * span_y):
                horizontal_strokes += 1
                has_long_horizontal |= span_x >= 0.07
            elif span_y >= max(0.01, 2 * span_x):
                vertical_strokes += 1
                has_long_vertical |= span_y >= 0.07
    # A sparse dimension can consist of a long line and two short perpendicular
    # witness lines. A paragraph divider on its own does not have that structure.
    has_dimension_strokes = (
        (has_long_horizontal and vertical_strokes >= 2)
        or (has_long_vertical and horizontal_strokes >= 2)
    )
    return (has_graphics and not has_text) or has_dimension_strokes


def vision_regions(image: Image.Image) -> list[VisionRegion]:
    """Return a page overview and up to four overlapping detail crops."""
    width, height = image.size
    overview = image.copy()
    overview.thumbnail((_OVERVIEW_EDGE, _OVERVIEW_EDGE), Image.Resampling.LANCZOS)
    regions = [VisionRegion("page", (0.0, 0.0, 1.0, 1.0), overview)]
    if max(width, height) <= _OVERVIEW_EDGE:
        return regions
    for row, (y0, y1) in enumerate(_detail_intervals(height), start=1):
        for column, (x0, x1) in enumerate(_detail_intervals(width), start=1):
            tile = image.crop((x0, y0, x1, y1))
            tile.thumbnail((_DETAIL_EDGE, _DETAIL_EDGE), Image.Resampling.LANCZOS)
            box = (x0 / width, y0 / height, x1 / width, y1 / height)
            regions.append(VisionRegion(f"tile-{row}-{column}", box, tile))
    return regions


def _detail_intervals(length: int) -> list[tuple[int, int]]:
    if length <= _OVERVIEW_EDGE:
        return [(0, length)]
    # Give a label or line near the middle room to appear fully in either tile.
    overlap_half = length // 16
    middle = length // 2
    return [(0, middle + overlap_half), (middle - overlap_half, length)]
