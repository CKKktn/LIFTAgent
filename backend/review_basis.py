"""Page images for uploaded review bases, kept separate from retrieval chunks."""
from contextlib import closing, nullcontext
from pathlib import Path
import shutil
import tempfile
from xml.sax.saxutils import escape

from .drawing_evidence import _image_block
from .drawing_sources import (
    DRAWING_IMAGE_EXTENSIONS, OFFICE_IMAGE_EXTENSIONS, drawing_pages, vision_regions,
    _PDF_LOCK, _render_drawing,
)
from .parsers import convert_office


def _render_text(knowledge, row, destination):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    style = ParagraphStyle("ContractText", fontName="STSong-Light", fontSize=11,
                           leading=18, wordWrap="CJK")
    story = []
    for document in knowledge.text_documents(row, allow_empty=True):
        for line in document.page_content.splitlines():
            if line.strip():
                for start in range(0, len(line), 2000):
                    story.append(Paragraph(escape(line[start:start + 2000]), style))
                story.append(Spacer(1, 5))
    if not story:
        if row["extension"] in OFFICE_IMAGE_EXTENSIONS:
            return False
        raise ValueError("审查依据没有可呈现的文字，请上传 PDF 或图片。")
    SimpleDocTemplate(str(destination)).build(story)
    return True


def _page_source(knowledge, row):
    original = knowledge.file_path(row)
    if row["extension"] == ".pdf" or row["extension"] in DRAWING_IMAGE_EXTENSIONS:
        return original, False
    version = knowledge._review_source_version(row)
    office = row["extension"] in OFFICE_IMAGE_EXTENSIONS | {".doc", ".xls", ".ppt"}
    reflow = not office or not (knowledge.settings.libreoffice_path or shutil.which("soffice") or shutil.which("libreoffice"))
    mode = "reflow" if reflow else "native"
    destination = knowledge.settings.data_dir / "previews" / f"basis-{row['id']}-{version[:20]}-{mode}.pdf"
    if not destination.is_file():
        with tempfile.TemporaryDirectory(prefix="review-basis-") as temporary:
            prepared = Path(temporary) / "source.pdf"
            if office and not reflow:
                prepared = convert_office(original, temporary, "pdf", knowledge.settings.libreoffice_path)
            else:
                if not _render_text(knowledge, row, prepared):
                    return None, True
                reflow = True
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".pdf", delete=False) as stream:
                staged = Path(stream.name)
            try:
                shutil.copyfile(prepared, staged)
                staged.replace(destination)
            finally:
                staged.unlink(missing_ok=True)
    return destination, reflow


def visual_basis_batches(knowledge, row):
    source, reflow = _page_source(knowledge, row)
    batches = []
    version = knowledge._review_source_version(row)
    context = closing(drawing_pages(source, text_pages=set(), force_pdf=True)) if source else nullcontext(())
    with context as pages:
        for page in pages:
            with page.image:
                batches.append({"key": f"{row['id']}:V{len(batches)+1}", "document_id": row["id"],
                                "source_version": version, "visual_page": page.number,
                                "source_part": "", "depth": 1, "reflow": reflow})
    if reflow and row["extension"] in OFFICE_IMAGE_EXTENSIONS:
        with closing(drawing_pages(knowledge.file_path(row), text_pages=set())) as pages:
            for page in pages:
                with page.image:
                    batches.append({"key": f"{row['id']}:V{len(batches)+1}", "document_id": row["id"],
                                    "source_version": version, "visual_page": page.number,
                                    "source_part": page.source_part, "depth": 1, "reflow": True})
    if not batches:
        raise ValueError("审查依据未取得可读取的页面，请检查文件。")
    return batches


def visual_basis_blocks(knowledge, row, batch):
    source = knowledge.file_path(row) if batch.get("source_part") else _page_source(knowledge, row)[0]
    if source.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        with _PDF_LOCK, closing(pdfium.PdfDocument(source)) as pdf:
            with closing(pdf[batch["visual_page"] - 1]) as page:
                image = _render_drawing(page, has_extracted_text=True, force_pdf=True)
        if image is None:
            raise ValueError("审查依据页面没有可见内容。")
        with image:
            return _region_blocks(image)
    with closing(drawing_pages(source, text_pages=set(), force_pdf=True,
                               source_part=batch.get("source_part", ""))) as pages:
        for page in pages:
            with page.image:
                if not batch.get("source_part") and page.number != batch["visual_page"]:
                    continue
                return _region_blocks(page.image)
    raise ValueError("审查依据页面已变更或不可用，请重新上传。")


def _region_blocks(image):
    regions = vision_regions(image)
    try:
        blocks = []
        for region in regions:
            blocks.extend(({"type": "text", "text": "页面区域 " + region.id}, _image_block(region)))
        return blocks
    finally:
        for region in regions:
            region.image.close()
