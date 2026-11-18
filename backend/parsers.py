from __future__ import annotations

from html import escape
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".yaml", ".yml", ".log"}
MODERN_OFFICE = {".docx", ".xlsx", ".pptx"}
LEGACY_OFFICE = {".doc", ".xls", ".ppt", ".odt", ".ods", ".odp", ".rtf"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | MODERN_OFFICE | LEGACY_OFFICE | {".pdf"}
CHUNK_METHODS = {"naive", "one", "laws", "table"}


def guard_office(path):
    with zipfile.ZipFile(path) as archive:
        files = archive.infolist()
        if len(files) > 20000 or sum(item.file_size for item in files) > 200 * 1024 * 1024:
            raise ValueError("Office 文件展开后过大，请拆分后上传")


def convert_office(path, directory, target, configured_binary=""):
    executable = configured_binary or shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise ValueError("此格式需要 LibreOffice，请配置 LIBREOFFICE_PATH，或另存为 DOCX/XLSX/PPTX/PDF 后上传")
    profile = (Path(directory) / "lo-profile").resolve().as_uri()
    process = subprocess.run([str(executable), "-env:UserInstallation=" + profile, "--headless", "--convert-to", target,
        "--outdir", str(directory), str(path)], capture_output=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = Path(directory) / (path.stem + "." + target)
    if process.returncode or not result.is_file():
        raise ValueError("Office 转换失败，请检查文件是否受密码保护或另存为 PDF")
    return result


def extract(path: Path, max_characters=2_000_000, libreoffice=""):
    extension = path.suffix.lower()
    output = []
    if extension in TEXT_EXTENSIONS:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030", errors="replace")
        output.append(Document(page_content=text, metadata={"source_kind": "text"}))
    elif extension == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 受密码保护，请先解密")
        if len(reader.pages) > 1500:
            raise ValueError("PDF 超过 1500 页，请拆分后上传")
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                output.append(Document(page_content=text, metadata={"page_number": index + 1, "source_kind": "pdf"}))
    elif extension == ".docx":
        from docx import Document as WordDocument
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        guard_office(path)
        document = WordDocument(path)
        parts = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                parts.append(Paragraph(child, document).text)
            elif child.tag.endswith("}tbl"):
                table = Table(child, document)
                parts.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
        output.append(Document(page_content="\n".join(parts), metadata={"source_kind": "word"}))
    elif extension == ".xlsx":
        from openpyxl import load_workbook
        guard_office(path)
        workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in workbook:
                lines = ["工作表：" + sheet.title]
                for index, row in enumerate(sheet.iter_rows(values_only=True)):
                    if index > 100000:
                        raise ValueError("单个工作表超过十万行，请拆分后上传")
                    if any(value is not None for value in row):
                        lines.append(" | ".join("" if value is None else str(value) for value in row))
                output.append(Document(page_content="\n".join(lines), metadata={"sheet": sheet.title, "source_kind": "spreadsheet"}))
        finally:
            workbook.close()
    elif extension == ".pptx":
        from pptx import Presentation
        guard_office(path)
        for index, slide in enumerate(Presentation(path).slides):
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text)
                if shape.has_table:
                    parts.extend(" | ".join(cell.text for cell in row.cells) for row in shape.table.rows)
            output.append(Document(page_content="\n".join(parts), metadata={"slide_number": index + 1, "source_kind": "presentation"}))
    elif extension in LEGACY_OFFICE:
        with tempfile.TemporaryDirectory(prefix="contract-convert-") as temporary:
            target = ".xlsx" if extension in {".xls", ".ods"} else ".pptx" if extension in {".ppt", ".odp"} else ".docx"
            converted = convert_office(path, temporary, target[1:], libreoffice)
            return extract(converted, max_characters, libreoffice)
    else:
        raise ValueError("暂不支持该文件类型")
    output = [Document(page_content=item.page_content.replace("\x00", "").strip(), metadata=item.metadata) for item in output if item.page_content.strip()]
    if not output:
        raise ValueError("未提取到文字。扫描 PDF 或图片需要先 OCR，再上传带文本层的 PDF 或 Word")
    if sum(len(item.page_content) for item in output) > max_characters:
        raise ValueError("提取文字超过两百万字符，请拆分材料")
    return output


def split_documents(documents, method, parser_config):
    if method not in CHUNK_METHODS:
        raise ValueError("不支持该分块方法")
    if method == "one":
        return documents
    chunk_size = max(200, min(int(parser_config.get("chunk_size", 1000)), 6000))
    overlap = max(0, min(int(parser_config.get("chunk_overlap", 120)), chunk_size - 1))
    separators = ["\n\n", "\n", "。", "；", " ", ""]
    if method == "laws":
        separators = [r"\n(?=第[一二三四五六七八九十百零〇0-9]+条)", "\n\n", "\n", "。", "", ]
    if method == "table":
        separators = ["\n", " | ", " ", ""]
        overlap = 0
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap,
        separators=separators, is_separator_regex=method == "laws", add_start_index=True)
    return splitter.split_documents(documents)


def text_preview_pdf(name, documents, destination):
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    style = ParagraphStyle("Chinese", fontName="STSong-Light", fontSize=10, leading=17, wordWrap="CJK", alignment=TA_LEFT)
    heading = ParagraphStyle("ChineseTitle", parent=style, fontSize=15, leading=23, spaceAfter=14)
    parts = [Paragraph(escape(name), heading), Paragraph("文字内容预览（重新排版，不代表原件页码或版式）。原文件可通过下载查看。", style), Spacer(1, 16)]
    text = "\n\n".join(item.page_content for item in documents)
    truncated = len(text) > 150000
    for paragraph in text[:150000].split("\n"):
        if paragraph.strip():
            for start in range(0, len(paragraph), 2000):
                parts.append(Paragraph(escape(paragraph[start:start + 2000]), style))
    if truncated:
        parts.append(Paragraph("预览仅显示前十五万字符，请下载原件查看全部内容。", style))
    SimpleDocTemplate(str(destination), pagesize=A4, title=name, author="LIFT Contract Review").build(parts)
    return destination
