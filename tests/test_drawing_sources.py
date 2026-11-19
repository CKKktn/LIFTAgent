import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from backend.drawing_sources import drawing_pages, vision_regions


def test_raster_page_applies_exif_orientation(tmp_path):
    source = tmp_path / "drawing.JPG"
    with Image.new("RGB", (120, 80), "white") as image:
        ImageDraw.Draw(image).rectangle((0, 0, 54, 34), fill="red")
        exif = image.getexif()
        exif[274] = 6
        image.save(source, exif=exif)

    page, = drawing_pages(source, text_pages=set())

    assert page.number == 1
    assert page.image.size == (80, 120)
    assert page.image.mode == "RGB"
    red, green, blue = page.image.getpixel((60, 20))
    assert red > 220 and green < 40 and blue < 40
    assert page.image.getpixel((20, 20)) == (255, 255, 255)


def test_transparent_drawing_renders_on_white(tmp_path):
    source = tmp_path / "drawing.png"
    image = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).line((10, 40, 110, 40), fill="black", width=2)
    image.save(source)

    page, = drawing_pages(source, text_pages=set())

    assert page.image.getpixel((0, 0)) == (255, 255, 255)
    assert page.image.getpixel((40, 40)) == (0, 0, 0)


def test_pdf_keeps_mixed_and_scanned_drawings_and_original_page_numbers(tmp_path):
    source = tmp_path / "mixed.pdf"
    pdf = Canvas(str(source), pagesize=(200, 120))
    pdf.drawString(15, 80, "Text-only page with a divider")
    pdf.line(15, 75, 185, 75)
    pdf.showPage()
    pdf.showPage()
    pdf.drawString(15, 100, "Vector drawing")
    pdf.rect(20, 15, 130, 65)
    pdf.circle(55, 45, 9)
    pdf.circle(110, 45, 9)
    pdf.showPage()
    with Image.new("RGB", (180, 100), "white") as scan:
        ImageDraw.Draw(scan).rectangle((0, 0, 65, 50), fill="red")
        pdf.drawImage(ImageReader(scan), 10, 10, 180, 100)
        pdf.showPage()
        pdf.drawString(15, 105, "Embedded drawing with a text layer")
        pdf.drawImage(ImageReader(scan), 10, 10, 140, 80)
        pdf.showPage()
    pdf.save()

    pages = list(drawing_pages(source, text_pages={1, 3, 5}))

    assert [page.number for page in pages] == [3, 4, 5]
    assert all(page.image.mode == "RGB" for page in pages)


def test_pdf_renderer_follows_display_rotation(tmp_path):
    unrotated = tmp_path / "before_rotation.pdf"
    pdf = Canvas(str(unrotated), pagesize=(260, 120))
    pdf.setFillColorRGB(1, 0, 0)
    pdf.rect(0, 0, 85, 35, fill=1, stroke=0)
    pdf.save()
    source = tmp_path / "rotated.pdf"
    reader = PdfReader(unrotated)
    writer = PdfWriter()
    writer.add_page(reader.pages[0].rotate(90))
    writer.write(source)

    page, = drawing_pages(source, text_pages=set())

    width, height = page.image.size
    assert height / width == pytest.approx(260 / 120, rel=0.01)
    assert page.image.getpixel((width // 10, height // 10)) == (255, 0, 0)
    assert page.image.getpixel((width // 10, height * 9 // 10)) == (255, 255, 255)


def test_pdf_with_sparse_dimension_strokes_is_a_drawing_candidate(tmp_path):
    source = tmp_path / "dimensions.pdf"
    pdf = Canvas(str(source), pagesize=(600, 400))
    pdf.drawString(240, 218, "120 mm")
    pdf.line(120, 210, 450, 210)
    pdf.line(120, 200, 120, 218)
    pdf.line(450, 200, 450, 218)
    pdf.line(120, 210, 128, 213)
    pdf.line(450, 210, 442, 213)
    pdf.save()

    assert [page.number for page in drawing_pages(source, text_pages={1})] == [1]


def test_pdf_override_renders_text_pages_but_skips_blank_pages(tmp_path):
    source = tmp_path / "text.pdf"
    pdf = Canvas(str(source), pagesize=(200, 120))
    pdf.drawString(15, 80, "Drawing labels in a native text layer")
    pdf.showPage()
    pdf.showPage()
    pdf.save()

    assert list(drawing_pages(source, text_pages={1})) == []
    assert [page.number for page in drawing_pages(source, text_pages={1}, force_pdf=True)] == [1]


@pytest.mark.parametrize("file_format", ["pdf", "png"])
def test_bad_drawing_file_is_skipped(tmp_path, file_format):
    source = tmp_path / f"invalid.{file_format}"
    source.write_bytes(b"This is a corrupt drawing file")

    assert list(drawing_pages(source, text_pages=set())) == []


def test_password_protected_pdf_is_skipped(tmp_path):
    source = tmp_path / "locked.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("private-test-password")
    writer.write(source)

    assert list(drawing_pages(source, text_pages=set())) == []


def test_overlapping_detail_regions_have_exact_page_pixel_coordinates():
    image = Image.new("RGB", (2909, 2381), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((1200, 1000, 1650, 1380), fill="red")
    draw.line((0, 41, 2890, 1900), fill="blue", width=5)

    overview, *tiles = vision_regions(image)

    assert overview.id == "page"
    assert overview.box == (0, 0, 1, 1)
    assert max(overview.image.size) < max(image.size)
    assert len(tiles) == 4
    assert len({tile.id for tile in tiles}) == 4
    assert tiles[0].box[2] > tiles[1].box[0]
    assert tiles[0].box[3] > tiles[2].box[1]
    assert tiles[0].box[:2] == (0, 0)
    assert tiles[-1].box[2:] == (1, 1)
    for tile in tiles:
        x0, y0, x1, y1 = tile.box
        expected = image.crop((round(x0 * 2909), round(y0 * 2381), round(x1 * 2909), round(y1 * 2381)))
        assert tile.image.size == expected.size
        assert max(tile.image.size) <= 1792
        assert tile.image.tobytes() == expected.tobytes()


def test_small_page_needs_only_the_overview():
    overview, = vision_regions(Image.new("RGB", (620, 400), "navy"))

    assert overview.id == "page"
    assert overview.image.size == (620, 400)


def test_large_source_has_bounded_page_and_tiles(tmp_path):
    source = tmp_path / "large.png"
    Image.new("RGB", (6000, 2100), "black").save(source)

    page, = drawing_pages(source, text_pages=set())
    overview, first, second = vision_regions(page.image)

    assert max(page.image.size) <= 3072
    assert max(overview.image.size) <= 1280
    assert max(first.image.size) <= 1792
    assert max(second.image.size) <= 1792
    assert first.box[2] > second.box[0]
