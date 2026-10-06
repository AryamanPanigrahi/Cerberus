import numpy as np
import pymupdf
import pytest
from PIL import Image

from cerberus.extract import figures, ocr
from cerberus.extract.ir import BlockType, Container
from cerberus.extract.pdf import extract_pdf

# All names and identifiers below are invented.

try:
    ocr.engine_version()
    HAVE_TESSERACT = True
except RuntimeError:
    HAVE_TESSERACT = False
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract is not installed")


def blank_page() -> np.ndarray:
    return np.full((1000, 800), 255, dtype=np.uint8)


def draw_frame(page: np.ndarray, x0: int, y0: int, x1: int, y1: int, level: int = 150) -> None:
    page[y0, x0 : x1 + 1] = page[y1, x0 : x1 + 1] = level
    page[y0 : y1 + 1, x0] = page[y0 : y1 + 1, x1] = level


def test_a_rectangle_of_thin_lines_is_a_frame():
    page = blank_page()
    draw_frame(page, 100, 300, 700, 600)
    assert figures.find_frames(page) == [(100, 300, 700, 600)]


def test_table_rules_and_underlines_are_not_frames():
    page = blank_page()
    page[100, 100:700] = 150  # a heading underline
    page[200:240, 100:700] = 40  # a dark table header band
    for y in (300, 360, 420):  # row rules, with no vertical lines joining them
        page[y, 100:700] = 200
    assert figures.find_frames(page) == []


def test_only_the_outermost_frame_is_returned():
    page = blank_page()
    draw_frame(page, 100, 300, 700, 700)
    draw_frame(page, 250, 400, 550, 600)  # a bordered picture inside the figure
    assert figures.find_frames(page) == [(100, 300, 700, 700)]


def test_prepare_crops_enlarges_and_makes_dark_panels_light():
    figure = Image.new("L", (400, 200), 255)
    figure.paste(20, (100, 50, 300, 150))  # a dark panel, like a terminal window
    figure.paste(230, (150, 99, 250, 101))  # a thin light stroke on it, like a letter
    image, scale, left, top = figures.prepare(figure)  # no scale given: guessed from width
    assert (left, top) == (100, 50)
    assert image.size == (200 * scale, 100 * scale) and scale == figures._MAX_SCALE
    pixels = np.asarray(image)
    stroke = pixels[pixels.shape[0] // 2, pixels.shape[1] // 2]
    panel = pixels[pixels.shape[0] // 8, pixels.shape[1] // 8]
    assert stroke < 60 and panel > 200  # now a dark stroke on a light background


@pytest.fixture
def scanned_with_figure(tmp_path):
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 100), "Badges are personal data and must be handled as such.", fontsize=11)
    page.draw_rect(pymupdf.Rect(72, 150, 520, 300), color=(0.6, 0.6, 0.6), width=1)
    page.insert_text((200, 210), "ACCESS BADGE", fontsize=6)
    page.insert_text((200, 225), "Jane Sample", fontsize=6)
    page.insert_text((200, 240), "VND-00002", fontsize=6)
    page.insert_text((72, 320), "Figure 1 - An issued access badge.", fontsize=9)
    pixmap = source[0].get_pixmap(dpi=200)
    pdf = pymupdf.open()
    scan = pdf.new_page()
    scan.insert_image(scan.rect, pixmap=pixmap)
    path = tmp_path / "figure.pdf"
    pdf.save(str(path))
    return extract_pdf(path)


@needs_tesseract
def test_figure_text_is_read_enlarged_and_marked_as_image_content(scanned_with_figure):
    doc = scanned_with_figure
    (region,) = [b for b in doc.blocks if b.type is BlockType.IMAGE_REGION]
    inside = doc.children(region.block_id)
    text = " ".join(b.text for b in inside)
    assert "Jane Sample" in text and "VND-00002" in text
    assert all(b.container is Container.IMAGE for b in inside)
    assert not region.quarantined

    # Nothing from inside the frame leaks into the body text, and the figure
    # sits between the paragraph above it and its caption.
    body = [b for b in doc.blocks if b.container is Container.BODY]
    assert not any("VND-00002" in b.text for b in body)
    above, caption = (b for b in body if b.type is BlockType.PARAGRAPH)
    assert above.order < region.order < caption.order and caption.text.startswith("Figure 1")

    # Word boxes are back in page coordinates: inside the frame that was drawn.
    (line,) = [b for b in inside if "VND-00002" in b.text]
    box = line.span_boxes(line.text.index("VND"), len(line.text))[0]
    assert 195 < box.x0 < 210 and 228 < box.y0 < 242
    assert 68 < region.bbox.x0 < 76 and 146 < region.bbox.y0 < 154


def test_a_header_band_counts_as_one_rule_below_its_text():
    page = blank_page()
    page[200:241, 100:700] = 40  # dark header band
    page[212:224, 150:300] = 255  # white header text breaks the band's rows
    page[300, 100:700] = 200  # faint row rules
    page[360, 100:700] = 200
    assert figures.horizontal_rules(page) == [
        (224, 100, 699, 200),  # the band: one rule under its text, remembering its top
        (300, 100, 699, None),
        (360, 100, 699, None),
    ]
