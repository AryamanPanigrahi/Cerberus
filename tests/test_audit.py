import csv

from cerberus.detect.finding import make
from cerberus.extract.ir import BBox, BlockType, Container, Document, Page, Source, Unit, Word
from cerberus.redact import tokens
from cerberus.report import audit

# All names and identifiers below are invented.


def sample():
    scan = Document("d0c", "policy.pdf", "pdf")
    scan.pages = [Page(1, 600, 800, Unit.PT, source=Source.OCR), Page(2, 600, 800, Unit.PT, source=Source.OCR),
                  Page(3, 600, 800, Unit.PT)]
    box = BBox(10, 20, 110, 32, Unit.PT)
    prose = scan.add(BlockType.PARAGRAPH, "It was approved by Jane Sample (EMP-00001) after a long review of the file.",
                     page=1, source=Source.OCR, section_path=("4. Approvals", "4.2 Sign-off"),
                     words=[Word(19, 23, box, 0.9), Word(24, 30, BBox(112, 20, 160, 32, Unit.PT), 0.9)])
    picture = scan.add(BlockType.IMAGE_REGION, container=Container.IMAGE, page=2, quarantined=True)
    line = scan.add(BlockType.PARAGRAPH, "Omar Placeholder", container=Container.IMAGE, page=2,
                    source=Source.OCR, parent_id=picture.block_id)
    deck = Document("d3ck", "people.pptx", "pptx")
    deck.pages = [Page(1, 9_000_000, 5_000_000, Unit.EMU)]
    cell = deck.add(BlockType.TABLE_CELL, "Jane Sample", container=Container.TABLE, page=1)

    def person(doc, block, start, end, location):
        return make(doc, block, start, end, entity="PERSON", detector="ner", rule="model", confidence=0.41,
                    location=location)

    found = [
        person(scan, prose, 19, 30, "prose"),
        make(scan, prose, 32, 41, entity="STAFF_ID", detector="rules", rule="staff_id", confidence=0.9,
             location="prose", flags=("ocr",)),
        person(scan, line, 0, 16, "image"),
        person(deck, cell, 0, 11, "table"),
    ]
    tokenized, _ = tokens.assign(found)
    return [scan, deck], tokenized


SECRETS = ("Jane", "Sample", "Omar", "Placeholder", "EMP-")


def test_one_row_per_finding_with_its_tags_and_no_value():
    docs, tokenized = sample()
    rows = audit.rows(docs, tokenized)
    assert [(r["file"], r["page"], r["location"], r["entity"], r["token"]) for r in rows] == [
        ("policy.pdf", 1, "prose", "PERSON", "<PERSON_1>"),
        ("policy.pdf", 1, "prose", "STAFF_ID", "<STAFF_ID_1>"),
        ("policy.pdf", 2, "image", "PERSON", "<PERSON_2>"),
        ("people.pptx", 1, "table", "PERSON", "<PERSON_1>"),
    ]
    first = rows[0]
    assert (first["section"], first["detector"], first["rule"], first["confidence"], first["read_by"]) == (
        "4. Approvals > 4.2 Sign-off", "ner", "model", 0.41, "ocr")
    assert first["box"] == "(10, 20)-(160, 32) pt"  # the two words of the name, as one rectangle
    assert rows[1]["flags"] == "ocr" and list(first) == audit.COLUMNS
    assert [s for s in SECRETS if any(s in str(v) for row in rows for v in row.values())] == []


def test_file_summary_counts_by_category_location_and_page():
    docs, tokenized = sample()
    text = audit.summary(docs[0], tokenized)
    assert "| Pages read by OCR | 2 (1-2) |" in text
    assert "| Pictures the OCR could not read reliably | 1 |" in text
    assert "| PERSON | 2 | 2 | 0 | 0 | 1 | 1 |" in text
    assert "| **All** | 3 | 3 | 0 | 0 | 2 | 1 |" in text
    assert "| 1 | 2 | 0 | 0 | 2 | 0 |" in text and "| 2 | 1 | 0 | 0 | 0 | 1 |" in text
    assert "| flag: ocr | 1 |" in text and "| found by: ner | 2 |" in text
    assert "## By slide" in audit.summary(docs[1], tokenized)
    assert [s for s in SECRETS if s in text] == []


def test_exposure_report_links_files_and_lists_picture_only_identities():
    docs, tokenized = sample()
    text = audit.exposure(docs, tokenized, verified=True)
    assert "| `<PERSON_1>` | people.pptx, policy.pdf |" in text
    assert "1 of 3 identities appear in no ordinary text" in text
    assert "| policy.pdf | 3 | 3 | 0 | 0 | 2 | 1 |" in text and "**passed**" in text
    assert "FAILED" in audit.exposure(docs, tokenized, verified=False)
    assert [s for s in SECRETS if s in text] == []


def test_a_value_in_a_heading_is_shown_as_its_token():
    doc = Document("d0c", "minutes.docx", "docx")
    doc.add(BlockType.HEADING, "Approvals: Jane Sample")
    block = doc.add(BlockType.PARAGRAPH, "Signed by Jane Sample.", section_path=("Approvals: Jane Sample",))
    found = [make(doc, block, 10, 21, entity="PERSON", detector="ner", rule="model", confidence=0.4, location="body")]
    tokenized, _ = tokens.assign(found)
    assert audit.rows([doc], tokenized)[0]["section"] == "Approvals: <PERSON_1>"


def test_files_are_written(tmp_path):
    docs, tokenized = sample()
    written = audit.write(tmp_path / "report", docs, tokenized, verified=True)
    assert [p.name for p in written] == ["audit.csv", "policy.summary.md", "people.summary.md", "exposure.md"]
    with open(written[0], encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 4
