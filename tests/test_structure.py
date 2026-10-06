from cerberus.eval import structure
from cerberus.eval.structure import parse_markdown, score
from cerberus.extract.ir import BlockType, Container, Document, Page, Unit

# All names and identifiers below are invented.

REFERENCE = """\
<!-- sample.pdf, unit 1 -->
# 3. Vendor contacts
## 3.1 Named staff
Each provider supplies a named roster every quarter.
- Collect the roster.
- Check it against the register.

| NAME | VENDOR ID |
| Jane Sample | VND-00000 |
| Omar Placeholder | VND-00001 |

[image]
> ACCESS BADGE
> Jane Sample
"""


def body(elements):
    return [e for e in elements if not e.image]


def test_reference_format_is_parsed_into_elements_with_parents():
    found = [(e.kind, e.text, e.parent) for e in parse_markdown(REFERENCE)]
    assert found[:5] == [
        ("h1", "3. Vendor contacts", ("under", "")),
        ("h2", "3.1 Named staff", ("under", "3. Vendor contacts")),
        ("paragraph", "Each provider supplies a named roster every quarter.", ("under", "3.1 Named staff")),
        ("list_item", "Collect the roster.", ("under", "3.1 Named staff")),
        ("list_item", "Check it against the register.", ("under", "3.1 Named staff")),
    ]
    # A cell's parent is its column header and the first cell of its row.
    assert ("cell", "VND-00001", ("cell", "VENDOR ID", "Omar Placeholder")) in found
    assert ("cell", "NAME", ("cell", "", "NAME")) in found
    assert found[-1] == ("paragraph", "Jane Sample", ("box", "ACCESS BADGE"))
    assert [e.image for e in parse_markdown(REFERENCE)][-2:] == [True, True]


def test_identical_extraction_scores_full_marks():
    reference = body(parse_markdown(REFERENCE))
    result = score(reference, body(parse_markdown(REFERENCE)))
    assert (result.total, result.retained, result.share) == (11, 11, 1.0)
    assert result.character_accuracy == 1.0
    assert result.by_kind == {"heading": [2, 2], "paragraph": [1, 1], "list_item": [2, 2], "cell": [6, 6]}


def test_ocr_slips_cost_character_accuracy_not_structure():
    extracted = REFERENCE.replace("VND-00001", "VND-O0001").replace("roster every", "roster evry")
    result = score(body(parse_markdown(REFERENCE)), body(parse_markdown(extracted)))
    assert result.retained == result.total
    assert 0.95 < result.character_accuracy < 1.0


def test_each_kind_of_damage_is_counted():
    reference = body(parse_markdown(REFERENCE))

    missing = REFERENCE.replace("- Collect the roster.\n", "")
    assert score(reference, body(parse_markdown(missing))).missing == 1

    # A list item read as a plain paragraph.
    wrong_kind = REFERENCE.replace("- Check it", "Check it")
    assert score(reference, body(parse_markdown(wrong_kind))).wrong_kind == 1

    # The sub-heading read as a top-level heading: it and what sits under it lose their parent.
    promoted = REFERENCE.replace("## 3.1", "# 3.1")
    result = score(reference, body(parse_markdown(promoted)))
    assert result.wrong_kind == 1 and result.wrong_parent == 0  # texts under it still name it

    # Two table columns swapped: every cell is now under the wrong header.
    swapped = REFERENCE.replace("| Jane Sample | VND-00000 |", "| VND-00000 | Jane Sample |")
    result = score(reference, body(parse_markdown(swapped)))
    assert result.missing + result.wrong_parent == 2 and result.retained == 9


def test_an_element_in_the_wrong_place_counts_as_missing():
    moved = REFERENCE.replace("Each provider supplies a named roster every quarter.\n", "") + (
        "\nEach provider supplies a named roster every quarter.\n"
    )
    result = score(body(parse_markdown(REFERENCE)), body(parse_markdown(moved)))
    assert result.missing == 1 and result.retained == 10


def test_text_inside_pictures_is_kept_apart():
    elements = parse_markdown(REFERENCE)
    pictures = [e for e in elements if e.image]
    assert [e.text for e in pictures] == ["ACCESS BADGE", "Jane Sample"]
    assert score(pictures, pictures).share == 1.0


def sample_document() -> Document:
    doc = Document("d0c", "sample.pdf", "pdf", pages=[Page(1, 595, 842, Unit.PT)])
    h1 = doc.add(BlockType.HEADING, "3. Vendor contacts", page=1)
    h2 = doc.add(BlockType.HEADING, "3.1 Named staff", page=1, parent_id=h1.block_id,
                 section_path=("3. Vendor contacts",))
    under = dict(page=1, parent_id=h2.block_id, section_path=("3. Vendor contacts", "3.1 Named staff"))
    doc.add(BlockType.PARAGRAPH, "Each provider supplies a named roster every quarter.", **under)
    doc.add(BlockType.LIST_ITEM, "Collect the roster.", **under)
    doc.add(BlockType.LIST_ITEM, "Check it against the register.", **under)
    table = doc.add(BlockType.TABLE, container=Container.TABLE, **under)
    headers = {}
    for r, row in enumerate([("NAME", "VENDOR ID"), ("Jane Sample", "VND-00000"),
                             ("Omar Placeholder", "VND-00001")]):
        for c, text in enumerate(row):
            cell = doc.add(BlockType.TABLE_CELL, text, container=Container.TABLE, page=1,
                           parent_id=table.block_id, row=r, col=c, header_id=headers.get(c))
            if r == 0:
                headers[c] = cell.block_id
    image = doc.add(BlockType.IMAGE_REGION, container=Container.IMAGE, **under)
    for text in ("ACCESS BADGE", "Jane Sample"):
        doc.add(BlockType.PARAGRAPH, text, container=Container.IMAGE, page=1, parent_id=image.block_id)
    doc.add(BlockType.FOOTER, "Page 1 of 1", page=1)  # page furniture: not structure
    return doc


def test_extraction_exports_to_the_reference_format_and_back():
    doc = sample_document()
    (blocks,) = structure.units(doc).values()
    exported = structure.to_markdown(doc, blocks)
    assert exported.strip() == REFERENCE.split("-->\n", 1)[1].strip()

    extracted = structure.elements(doc, blocks)
    as_tuples = lambda items: [(e.kind, e.text, e.parent, e.image) for e in items]  # noqa: E731
    assert as_tuples(parse_markdown(exported)) == as_tuples(extracted)
    assert "Page 1 of 1" not in exported


def test_docx_units_are_top_level_sections():
    doc = Document("d0c", "sample.docx", "docx")
    doc.add(BlockType.METADATA, "Jane Sample", native_ref={"core_property": "author"})
    doc.add(BlockType.PARAGRAPH, "Cover text")
    first = doc.add(BlockType.HEADING, "1. Scope")
    doc.add(BlockType.PARAGRAPH, "Body", parent_id=first.block_id, section_path=("1. Scope",))
    doc.add(BlockType.HEADING, "2. Roles")
    by_unit = structure.units(doc)
    assert {unit: [b.text for b in blocks] for unit, blocks in by_unit.items()} == {
        0: ["Cover text"], 1: ["1. Scope", "Body"], 2: ["2. Roles"],
    }
