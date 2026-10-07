import pymupdf
import pytest

from cerberus.detect import rules
from cerberus.detect.finding import make
from cerberus.extract import ocr
from cerberus.extract.ir import BlockType, Container, Document, Page, Source, Unit
from cerberus.extract.pdf import extract_pdf
from cerberus.redact import tokens
from cerberus.redact.pdf import mask_pdf
from cerberus.redact.text import redact_document, redacted_text

# All names and contact details below are invented.

try:
    ocr.engine_version()
    HAVE_TESSERACT = True
except RuntimeError:
    HAVE_TESSERACT = False
needs_tesseract = pytest.mark.skipif(not HAVE_TESSERACT, reason="Tesseract is not installed")


def person(doc, block, text):
    start = block.text.index(text)
    return make(doc, block, start, start + len(text), entity="PERSON", detector="test", rule="t",
                confidence=0.9, location="image" if block.container is Container.IMAGE else "prose")


def sample():
    doc = Document("d0c", "sample.pdf", "pdf", pages=[Page(1, 595, 842, Unit.PT), Page(2, 595, 842, Unit.PT)])
    heading = doc.add(BlockType.HEADING, "1. Approvals", page=1)
    under = dict(page=1, parent_id=heading.block_id, section_path=("1. Approvals",))
    para = doc.add(BlockType.PARAGRAPH, "Jane Sample approved it; J. Sample later revoked it. Write to "
                                        "j.sample@corp.example.", **under)
    table = doc.add(BlockType.TABLE, container=Container.TABLE, **under)
    cell = doc.add(BlockType.TABLE_CELL, "Omar Placeholder", container=Container.TABLE, page=1,
                   parent_id=table.block_id, row=0, col=0)
    picture = doc.add(BlockType.IMAGE_REGION, container=Container.IMAGE, **under)
    line = doc.add(BlockType.PARAGRAPH, "Badge of Jane Sample", container=Container.IMAGE, source=Source.OCR,
                   page=1, parent_id=picture.block_id)
    doc.add(BlockType.FOOTER, "Prepared by Jane Sample", page=1)
    doc.add(BlockType.METADATA, "Jane Sample", native_ref={"info": "author"})
    doc.add(BlockType.PARAGRAPH, "Second page text.", page=2)
    found = rules.detect([doc]) + [
        person(doc, para, "Jane Sample"), person(doc, para, "J. Sample"), person(doc, cell, "Omar Placeholder"),
        person(doc, line, "Jane Sample"),
    ]
    tokenized, _ = tokens.assign(found)
    return doc, tokenized


def test_tokens_replace_pii_in_place_and_keep_who_is_who():
    doc, tokenized = sample()
    text = redacted_text(doc, tokenized)
    assert "<PERSON_1> approved it; <PERSON_1> later revoked it. Write to <EMAIL_1>." in text
    assert "| <PERSON_2> |" in text  # a different person, a different token, still in its table
    assert text.startswith("<!-- page 1 -->\n# 1. Approvals")
    assert "<!-- page 2 -->\nSecond page text." in text


def test_no_original_value_survives_in_the_redacted_text():
    doc, tokenized = sample()
    text = redacted_text(doc, tokenized)
    for value in ("Jane", "Sample", "Placeholder", "j.sample", "corp.example"):
        assert value not in text


def test_picture_text_footers_and_properties_are_withheld():
    doc, tokenized = sample()
    text = redacted_text(doc, tokenized)
    assert "(picture withheld: 1 item of PII detected inside)" in text
    assert "Badge" not in text and "Prepared by" not in text


def test_redacted_copy_keeps_block_ids_so_it_stays_traceable():
    doc, tokenized = sample()
    clean = redact_document(doc, tokenized)
    assert [b.block_id for b in clean.blocks] == [b.block_id for b in doc.blocks]
    assert clean.validate() == []
    assert doc.blocks[1].text.startswith("Jane Sample")  # the original is untouched


def test_a_quarantined_page_is_withheld_whole():
    doc, tokenized = sample()
    doc.pages[1].quarantined = True
    text = redacted_text(doc, tokenized)
    assert "(page withheld: it could not be read reliably)" in text
    assert "Second page text." not in text


@pytest.fixture
def scanned(tmp_path):
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 100), "Contact j.sample@corp.example or +1 (555) 010-0101 today.", fontsize=11)
    page.insert_text((72, 140), "Nothing personal is on this line at all.", fontsize=11)
    pixmap = source[0].get_pixmap(dpi=200)
    pdf = pymupdf.open()
    scan = pdf.new_page()
    scan.insert_image(scan.rect, pixmap=pixmap)
    pdf.set_metadata({"author": "Jane Sample", "title": "Contacts"})
    path = tmp_path / "scan.pdf"
    pdf.save(str(path))
    return path


@needs_tesseract
def test_masked_pdf_removes_the_pixels_and_the_file_properties(scanned, tmp_path):
    doc = extract_pdf(scanned)
    tokenized, _ = tokens.assign(rules.detect([doc]))
    assert {t.finding.entity for t in tokenized} == {"EMAIL", "PHONE"}
    out = mask_pdf(scanned, doc, tokenized, tmp_path / "masked" / "scan.pdf")

    with pymupdf.open(str(out)) as masked, pymupdf.open(str(scanned)) as original:
        assert masked.page_count == original.page_count
        assert masked[0].rect == original[0].rect
        assert not any((masked.metadata or {}).get(k) for k in ("author", "title"))
        assert not masked.get_xml_metadata()
        assert "<EMAIL_1>" in masked[0].get_text() and "<PHONE_1>" in masked[0].get_text()

    # Reading the masked file afresh finds neither value, and the rest of the page is intact.
    again = extract_pdf(out)
    everything = " ".join(b.text for b in again.blocks)
    assert "j.sample" not in everything and "010-0101" not in everything
    assert "Jane Sample" not in everything
    assert rules.detect([again]) == []
    assert "Nothing personal is on this line" in everything
