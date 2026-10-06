import pymupdf
import pytest

from cerberus.extract import ocr
from cerberus.extract.ir import BlockType, Container, Source
from cerberus.extract.pdf import extract_pdf

# All names and identifiers below are invented.

try:
    ocr.engine_version()
    HAVE_TESSERACT = True
except RuntimeError:
    HAVE_TESSERACT = False
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract is not installed")


def typed_page(pdf) -> None:
    page = pdf.new_page()  # A4, 595 x 842 points
    page.insert_text((72, 90), "1. Vendor contacts", fontsize=20)
    page.insert_text((72, 130), "Each provider supplies a named roster every quarter.", fontsize=11)
    page.insert_text((72, 146), "The roster is itself personal data.", fontsize=11)
    for y, row in zip((200, 226, 252), [("NAME", "VENDOR ID", "LOCATION"),
                                        ("Jane Sample", "VND-00000", "Springfield"),
                                        ("Omar Placeholder", "VND-00001", "Shelbyville")]):
        for x, value in zip((80, 250, 400), row):
            page.insert_text((x, y), value, fontsize=11)


@pytest.fixture
def typed(tmp_path):
    pdf = pymupdf.open()
    typed_page(pdf)
    pdf.set_metadata({"author": "Jane Sample", "title": "Roster policy"})
    path = tmp_path / "typed.pdf"
    pdf.save(str(path))
    return path


@pytest.fixture
def scanned(tmp_path):
    """The same page, but as a picture with no text layer, like a scan."""
    source = pymupdf.open()
    typed_page(source)
    pixmap = source[0].get_pixmap(dpi=200)
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_image(page.rect, pixmap=pixmap)
    path = tmp_path / "scanned.pdf"
    pdf.save(str(path))
    return path


def test_native_page_is_read_without_ocr(typed):
    doc = extract_pdf(typed)
    assert doc.ocr_pages() == []
    assert doc.pages[0].source is Source.NATIVE
    (heading,) = [b for b in doc.blocks if b.type is BlockType.HEADING]
    assert heading.text == "1. Vendor contacts"
    body = " ".join(b.text for b in doc.blocks if b.type is BlockType.PARAGRAPH)
    assert "named roster every quarter" in body and "VND-00001" in body
    assert all(b.source is Source.NATIVE for b in doc.blocks)


def test_file_properties_are_emitted(typed):
    doc = extract_pdf(typed)
    info = {b.native_ref["info"]: b.text for b in doc.blocks if "info" in (b.native_ref or {})}
    assert info["author"] == "Jane Sample"
    assert info["title"] == "Roster policy"


@needs_tesseract
def test_scanned_page_goes_through_ocr(scanned):
    doc = extract_pdf(scanned)
    assert doc.ocr_pages() == [1]
    page = doc.pages[0]
    assert page.ocr_conf > 0.8 and not page.quarantined
    assert doc.versions["tesseract"]

    (heading,) = [b for b in doc.blocks if b.type is BlockType.HEADING]
    assert heading.text == "1. Vendor contacts"
    (paragraph,) = [b for b in doc.blocks if b.type is BlockType.PARAGRAPH]
    assert paragraph.text.startswith("Each provider") and paragraph.text.endswith("personal data.")
    assert paragraph.parent_id == heading.block_id


@needs_tesseract
def test_scanned_table_and_word_boxes(scanned):
    doc = extract_pdf(scanned)
    (table,) = [b for b in doc.blocks if b.type is BlockType.TABLE]
    cells = doc.children(table.block_id)
    assert [c.text for c in cells] == [
        "NAME", "VENDOR ID", "LOCATION",
        "Jane Sample", "VND-00000", "Springfield",
        "Omar Placeholder", "VND-00001", "Shelbyville",
    ]
    cell = cells[4]
    assert (cell.row, cell.col) == (1, 1) and cell.container is Container.TABLE
    assert doc.get(cell.header_id).text == "VENDOR ID"

    # The word's box is in PDF points and lands where the text was drawn (x=250, baseline y=226).
    (box,) = cell.span_boxes(0, len(cell.text))
    assert 245 < box.x0 < 255 and 210 < box.y0 < 226 <= box.y1 < 232


def test_encrypted_pdf_is_refused(tmp_path):
    pdf = pymupdf.open()
    typed_page(pdf)
    path = tmp_path / "locked.pdf"
    pdf.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    with pytest.raises(ValueError, match="encrypted"):
        extract_pdf(path)
