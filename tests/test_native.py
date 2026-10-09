import docx
import pytest
from pptx import Presentation
from pptx.util import Emu

from cerberus.eval import native, structure, synthetic
from cerberus.extract import extract
from cerberus.extract.ir import BlockType

# Every name below is invented.


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    folder = tmp_path_factory.mktemp("samples")
    return {path.name: path for path in synthetic.make(folder)}


def test_the_reference_is_read_from_the_docx_itself(tmp_path):
    d = docx.Document()
    d.add_heading("1. Scope", level=1)
    d.add_paragraph("This applies to all staff.")
    d.add_heading("1.1 Exceptions", level=2)
    d.add_paragraph("First exception", style="List Bullet")
    table = d.add_table(rows=2, cols=2)
    for cell, text in zip([c for row in table.rows for c in row.cells], ("Name", "Role", "Jane Sample", "Approver")):
        cell.text = text
    path = tmp_path / "policy.docx"
    d.save(str(path))

    reference = native.docx_reference(path)
    assert [(e.kind, e.text) for e in reference] == [
        ("h1", "1. Scope"), ("paragraph", "This applies to all staff."), ("h2", "1.1 Exceptions"),
        ("list_item", "First exception"), ("cell", "Name"), ("cell", "Role"), ("cell", "Jane Sample"), ("cell", "Approver"),
    ]
    assert reference[3].parent == ("under", "1.1 Exceptions")
    assert reference[7].parent == ("cell", "Role", "Jane Sample")  # its column header, and the first cell of its row

    result = native.score_docx(path, extract(path))
    assert (result.retained, result.total) == (8, 8)


def test_a_docx_extraction_that_loses_structure_scores_lower(samples):
    path = samples["staff_notice.docx"]
    doc = extract(path)
    assert native.score_docx(path, doc).share == 1.0

    # A heading read as a plain paragraph, and two paragraphs swapped.
    heading = next(b for b in doc.blocks if b.type is BlockType.HEADING)
    heading.type = BlockType.PARAGRAPH
    damaged = native.score_docx(path, doc)
    assert damaged.wrong_kind == 1 and damaged.share < 1.0


def test_shapes_on_a_slide_are_scored_by_slide_and_kind_not_by_order(samples):
    path = samples["team.pptx"]
    doc = extract(path)
    result = native.score_pptx(path, doc)
    assert (result.retained, result.total) == (13, 13)  # a title, three cards, nine cells
    assert "order not scored" in native.report("deck", result, ordered=False)

    # The file stores the three cards right to left; the reference does not care which way round.
    cards = [e.text for e in native.pptx_reference(path)[1] if e.kind == "paragraph"]
    assert [c.split()[0] for c in cards] == ["Ana", "Omar", "Jane"]


def test_text_put_on_the_wrong_slide_is_not_retained(tmp_path):
    prs = Presentation()
    for title in ("First", "Second"):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = title
        box = slide.shapes.add_textbox(Emu(500_000), Emu(2_000_000), Emu(4_000_000), Emu(500_000))
        box.text_frame.text = f"Body of the {title.lower()} slide"
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    doc = extract(path)
    assert native.score_pptx(path, doc).share == 1.0

    moved = next(b for b in doc.blocks if b.text == "Body of the second slide")
    moved.page = 1
    result = native.score_pptx(path, doc)
    assert result.missing == 1 and (result.retained, result.total) == (3, 4)


def test_the_unordered_pairing_prefers_the_copy_in_the_right_place():
    def cell(text, row, col):
        return structure.Element("cell", text, table=0, row=row, col=col)

    reference = structure._link([cell("Name", 0, 0), cell("Role", 0, 1), cell("Jane", 1, 0), cell("Jane", 1, 1)])
    extracted = structure._link([cell("Name", 0, 0), cell("Role", 0, 1), cell("Jane", 1, 0), cell("Jane", 1, 1)])
    assert native._unordered(reference, list(reversed(extracted))).retained == 4
