import pymupdf
import pytest
from PIL import Image

from cerberus import demo
from cerberus.detect import rules
from cerberus.eval import synthetic
from cerberus.extract import extract
from cerberus.extract.ir import BlockType
from cerberus.redact import tokens
from cerberus.redact.pdf import mask_pdf

# The sample files are generated: every name and number in them is invented.


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    folder = tmp_path_factory.mktemp("samples")
    return {path.name: path for path in synthetic.make(folder)}


def test_three_sample_files_are_written_one_per_format(samples):
    assert sorted(samples) == ["incident_scan.pdf", "staff_notice.docx", "team.pptx"]
    with pymupdf.open(str(samples["incident_scan.pdf"])) as pdf:
        assert len(pdf) == 3 and all(page.get_text().strip() == "" for page in pdf)  # scans: no text layer
        assert pdf.metadata["author"] == synthetic.AUTHOR


def test_the_sample_slide_stores_its_boxes_out_of_reading_order_and_extraction_restores_it(samples):
    doc = extract(samples["team.pptx"])
    boxes = [b.text.split("\n")[0] for b in doc.blocks if b.type is BlockType.TEXT_BOX]
    assert boxes == [person[0] for person in synthetic.PEOPLE]
    properties = [b.text for b in doc.blocks if b.type is BlockType.METADATA]
    assert synthetic.AUTHOR in properties and "Steve Canny" not in properties


def test_views_of_a_run_show_tokens_and_keep_values_out_unless_asked(samples):
    doc = extract(samples["staff_notice.docx"])
    tokenized, _ = tokens.assign(rules.detect([doc]))
    counted = demo.counts(tokenized)
    assert "EMAIL" in counted and "STAFF_ID" in counted and "<b>All</b>" in counted

    hidden = demo.findings([doc], tokenized)
    assert "&lt;EMAIL_1&gt;" in hidden and "j.sample@corp.example" not in hidden
    assert "j.sample@corp.example" in demo.findings([doc], tokenized, values=True)

    both = demo.text_views(doc, tokenized, limit=50)
    assert "<mark>j.sample@corp.example</mark>" in both and "<span class=tok>&lt;EMAIL_1&gt;</span>" in both
    # A file property is searched but never passed on, so its right-hand side says so.
    assert f"<td>file property</td><td>{synthetic.AUTHOR}</td><td><i>withheld</i></td>" in both


def test_a_greyscale_scan_is_still_the_same_page_after_masking(samples, tmp_path):
    """Regression: recompressing a greyscale page image once wrote it back as stripes."""
    source = samples["incident_scan.pdf"]
    doc = extract(source)
    if not any(b.text for b in doc.blocks if b.type is BlockType.PARAGRAPH):
        pytest.skip("Tesseract is not installed")
    tokenized, _ = tokens.assign(rules.detect([doc]))
    assert tokenized
    out = mask_pdf(source, doc, tokenized, tmp_path / "masked.pdf")

    def strip(path):  # the last line of the page, which holds no PII and must be unchanged
        with pymupdf.open(str(path)) as pdf:
            pixmap = pdf[0].get_pixmap(dpi=50, colorspace=pymupdf.csGRAY, clip=pymupdf.Rect(60, 255, 540, 285))
            return Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)

    before, after = strip(source), strip(out)
    differing = sum(abs(a - b) > 60 for a, b in zip(before.tobytes(), after.tobytes()))
    assert min(before.tobytes()) < 128  # there is text in the strip
    assert differing < 0.01 * before.width * before.height
    with pymupdf.open(str(out)) as pdf:
        assert not (pdf.metadata or {}).get("author")


def test_uploaded_files_replace_what_was_in_the_folder(tmp_path):
    (tmp_path / "old.pdf").write_bytes(b"%PDF-1.4 old")
    uploaded = (
        {"name": "new.docx", "content": memoryview(b"new document")},
        {"name": "notes.txt", "content": memoryview(b"not a supported type")},
    )
    assert [p.name for p in demo.save_uploads(uploaded, tmp_path)] == ["new.docx"]
    assert [p.name for p in demo.files_in(tmp_path)] == ["new.docx"]
    # Nothing usable uploaded: the folder is left as it was.
    assert demo.save_uploads(({"name": "notes.txt", "content": b"x"},), tmp_path) == []
    assert [p.name for p in demo.files_in(tmp_path)] == ["new.docx"]


def test_fast_mode_switches_the_second_engine_off_only_for_the_run(tmp_path, monkeypatch):
    import os

    seen = {}
    monkeypatch.setattr(demo.pipeline, "run", lambda paths, out, pictures: seen.update(mode=os.environ.get("CERBERUS_OCR")))
    monkeypatch.setenv("CERBERUS_OCR", "both")
    demo.run(tmp_path, tmp_path / "out", fast=True)
    assert seen["mode"] == "tesseract" and os.environ["CERBERUS_OCR"] == "both"
    demo.run(tmp_path, tmp_path / "out")
    assert seen["mode"] == "both"


def test_a_picture_is_fetched_by_its_place_in_the_document(samples):
    html = demo.pictures(samples["staff_notice.docx"], samples["staff_notice.docx"], number=1)
    assert html.count("data:image/png;base64,") == 2
    with pytest.raises(ValueError, match="shows 1 pictures"):
        demo.pictures(samples["staff_notice.docx"], samples["staff_notice.docx"], number=2)
