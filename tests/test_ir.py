from cerberus.extract.ir import (
    BBox,
    BlockType,
    Container,
    Document,
    Page,
    Source,
    Unit,
    Word,
    doc_id_from_bytes,
)


def px(x0, y0, x1, y1):
    return BBox(x0, y0, x1, y1, Unit.PX)


def sample_doc() -> Document:
    """A heading, a sentence and a 2x2 table on one OCR page. All values invented."""
    doc = Document(doc_id_from_bytes(b"synthetic"), "sample.pdf", "pdf")
    doc.pages.append(Page(1, 2480, 3508, Unit.PX, source=Source.OCR, ocr_conf=0.93))

    heading = doc.add(BlockType.HEADING, "4. Contacts", page=1)
    path = ("4. Contacts",)
    doc.add(
        BlockType.PARAGRAPH,
        "Reach the helpdesk by email.",
        parent_id=heading.block_id,
        page=1,
        section_path=path,
    )
    table = doc.add(BlockType.TABLE, parent_id=heading.block_id, page=1, section_path=path)

    def cell(text, row, col, header=None, words=()):
        return doc.add(
            BlockType.TABLE_CELL,
            text,
            container=Container.TABLE,
            source=Source.OCR,
            parent_id=table.block_id,
            page=1,
            row=row,
            col=col,
            header_id=header.block_id if header else None,
            words=list(words),
            section_path=path,
        )

    h_name = cell("Name", 0, 0, words=[Word(0, 4, px(100, 500, 180, 530), 0.97)])
    h_id = cell("Employee ID", 0, 1, words=[
        Word(0, 8, px(600, 500, 760, 530), 0.96),
        Word(9, 11, px(770, 500, 800, 530), 0.95),
    ])
    cell("Jane Sample", 1, 0, h_name, words=[
        Word(0, 4, px(100, 540, 170, 570), 0.94),
        Word(5, 11, px(180, 540, 290, 570), 0.92),
    ])
    cell("EMP-00000", 1, 1, h_id, words=[Word(0, 9, px(600, 540, 780, 570), 0.90)])
    return doc


def test_add_assigns_sequential_order_and_deterministic_ids():
    a, b = sample_doc(), sample_doc()
    assert [blk.order for blk in a.blocks] == list(range(1, 8))
    assert [blk.block_id for blk in a.blocks] == [blk.block_id for blk in b.blocks]
    assert a.get(a.blocks[4].block_id) is a.blocks[4]


def test_parent_is_container_not_previous_block():
    doc = sample_doc()
    table = doc.blocks[2]
    cells = doc.children(table.block_id)
    assert [c.text for c in cells] == ["Name", "Employee ID", "Jane Sample", "EMP-00000"]
    assert doc.get(cells[3].header_id).text == "Employee ID"


def test_span_boxes_maps_characters_to_word_rectangles():
    name_cell = sample_doc().blocks[5]
    surname = name_cell.text.index("Sample")
    assert name_cell.span_boxes(surname, surname + 6) == [px(180, 540, 290, 570)]
    assert len(name_cell.span_boxes(0, len(name_cell.text))) == 2
    assert name_cell.span_boxes(4, 5) == []  # the space between words


def test_json_round_trip_is_lossless():
    doc = sample_doc()
    assert Document.from_json(doc.to_json()) == doc


def test_valid_document_has_no_errors_and_reports_ocr_pages():
    doc = sample_doc()
    assert doc.validate() == []
    assert doc.ocr_pages() == [1]


def test_validate_flags_broken_invariants():
    doc = sample_doc()
    doc.blocks[1].parent_id = doc.blocks[6].block_id  # parent comes later
    doc.blocks[4].row = None  # cell without position
    doc.blocks[5].words = []  # OCR text that could not be masked
    doc.blocks[6].words = [Word(0, 20, px(0, 0, 1, 1), 0.9)]  # past end of text
    doc.blocks[3].page = 9  # page not in document

    errors = "\n".join(doc.validate())
    for expected in (
        "missing or not earlier",
        "cell without row/col",
        "OCR text without word boxes",
        "out of order or range",
        "page 9 not in document",
    ):
        assert expected in errors
