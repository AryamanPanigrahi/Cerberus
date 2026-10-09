import csv

import pytest

from cerberus.detect.finding import make
from cerberus.eval import ground_truth, recall
from cerberus.eval.ground_truth import Label
from cerberus.extract.ir import BlockType, Container, Document

# All names and identifiers below are invented.


def workbook(path, rows, sheet="Reader"):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.title = "Rules"
    tab = book.create_sheet(sheet)
    for row in rows:
        tab.append(row)
    book.save(path)
    return path


def test_labels_are_read_by_column_heading(tmp_path):
    path = workbook(tmp_path / "labels.xlsx", [
        ("#", "File", "Page / Slide", "Section", "Location", "Type", "Exact text", "Notes", "Checked by"),
        (1, "PDF", 3, "2.1", "Table", "person", " Jane Sample ", "owner", None),
        (2, "PDF", 4, "2.2", "prose", "EMAIL", "j.sample@corp.example", None, None),
        (3, "PDF", 4, None, None, None, None, None, None),  # an unfinished row
    ])
    assert ground_truth.load(path, "Reader") == [
        Label(row=2, page=3, type="PERSON", text="Jane Sample", location="table", section="2.1", file="PDF", notes="owner"),
        Label(row=3, page=4, type="EMAIL", text="j.sample@corp.example", location="prose", section="2.2", file="PDF"),
    ]
    with pytest.raises(ValueError, match="no tab named"):
        ground_truth.load(path, "Nobody")


def test_edit_distance_stops_counting_past_the_limit():
    assert recall.distance("emp-00001", "emp-00001") == 0
    assert recall.distance("jane sample", "jane sampie") == 1
    assert recall.distance("j.sample@corp.example", "jsample@corp.exampl") == 2
    assert recall.distance("jane sample", "omar placeholder") == 3


def test_a_value_is_located_exactly_or_approximately():
    text = "approved by jane sampie on monday"
    assert recall.locate("jane sampie", text) == (12, 23, 0)
    assert recall.locate("jane sample", text) == (12, 23, 1)
    assert recall.locate("omar placeholder", text) is None


def sample():
    doc = Document("d0c", "sample.pdf", "pdf")
    table = doc.add(BlockType.TABLE, container=Container.TABLE, page=3)
    cells = dict(container=Container.TABLE, parent_id=table.block_id, page=3)
    texts = ["Jane Sample", "EMP-00001", "Jane Sample", "j.sampie@corp.example", "Omar Q. Placeholder", "Risk Desk"]
    found = [
        make(doc, doc.add(BlockType.TABLE_CELL, text, **cells), 0, len(text), entity=entity, detector="rules",
             rule="fake", confidence=0.9, location="table")
        for text, entity in zip(texts, ["PERSON", "STAFF_ID", "PERSON", "EMAIL", "PERSON", "PERSON"])
    ]
    doc.add(BlockType.PARAGRAPH, "Ana Example signed. Her number is on file.", page=4)

    def label(row, page, kind, text, location="table"):
        return Label(row=row, page=page, type=kind, text=text, location=location)

    labels = [
        label(2, 3, "PERSON", "jane  sample"),  # case and spacing differ
        label(3, 3, "PERSON", "Jane Sample"),
        label(4, 3, "PERSON", "Jane Sample"),  # a third time: only two were found
        label(5, 3, "EMPLOYEE_ID", "EMP-00001"),
        label(6, 3, "EMAIL", "j.sample@corp.example"),  # OCR changed a letter
        label(7, 3, "PERSON", "Omar Placeholder"),  # found with a middle initial: most characters alike
        label(8, 4, "PERSON", "Ana Example", "prose"),  # in the text, not detected
        label(9, 4, "PHONE", "+00 0000 000000", "prose"),  # not in the text at all
    ]
    return doc, labels, found


def test_pairing_is_counted_and_strictest_first():
    doc, labels, found = sample()
    outcomes, unmatched = recall.compare(labels, found, recall.page_texts(doc))
    assert [(o.label.row, o.how, o.distance) for o in outcomes] == [
        (2, "exact", 0), (3, "exact", 0), (4, "missed", 0), (5, "exact", 0),
        (6, "near", 1), (7, "garbled", 0), (8, "missed", 0), (9, "missed", 0),
    ]
    assert [f.text for f in unmatched] == ["Risk Desk"]
    # A page number is part of the match: the same value on another page does not count.
    assert outcomes[2].in_text == "exact" and outcomes[6].in_text == "exact" and outcomes[7].in_text == "no"
    assert outcomes[6].context == "[[ana example]] signed. her number is on file."


def test_one_text_inside_the_other_is_a_partial_match():
    doc = Document("d0c", "sample.pdf", "pdf")
    block = doc.add(BlockType.PARAGRAPH, "Approved by Dr Omar Placeholder today.", page=1)
    found = [make(doc, block, 12, 31, entity="PERSON", detector="ner", rule="fake", confidence=0.4, location="prose")]
    labels = [Label(row=2, page=1, type="PERSON", text="Omar Placeholder", location="prose")]
    (outcome,), unmatched = recall.compare(labels, found, recall.page_texts(doc))
    assert (outcome.how, outcome.finding.text, unmatched) == ("partial", "Dr Omar Placeholder", [])


def test_the_summary_holds_counts_and_no_values(tmp_path):
    doc, labels, found = sample()
    outcomes, unmatched = recall.compare(labels, found, recall.page_texts(doc))
    text = recall.summary(outcomes, unmatched, len(found))
    assert [value for value in ("Sample", "Placeholder", "EMP-", "corp.example", "Risk Desk") if value in text] == []
    assert "paired with a label: 5" in text and "not in the extracted text (an extraction or OCR loss): 1" in text

    written = recall.write_report(tmp_path / "eval", doc, outcomes, unmatched, text)
    assert sorted(p.name for p in written) == [
        "labels.csv", "misses.csv", "near_misses.csv", "summary.txt", "unmatched_findings.csv",
    ]
    with open(tmp_path / "eval" / "misses.csv", encoding="utf-8-sig", newline="") as handle:
        assert [row["sheet_row"] for row in csv.DictReader(handle)] == ["4", "8", "9"]


def test_a_value_found_but_read_badly_is_garbled_and_kept_out_of_the_other_counts():
    doc = Document("d0c", "scan.pdf", "pdf")
    block = doc.add(BlockType.PARAGRAPH, "Write to j.samp1e@corq.exarnple or call.", page=1)
    found = [make(doc, block, 9, 31, entity="EMAIL", detector="rules", rule="fake", confidence=0.9, location="image")]
    labels = [Label(row=2, page=1, type="EMAIL", text="j.sample@corp.example", location="image"),
              Label(row=3, page=1, type="PHONE", text="+00 0000 000000", location="image")]
    outcomes, unmatched = recall.compare(labels, found, recall.page_texts(doc))
    assert [o.how for o in outcomes] == ["garbled", "missed"] and unmatched == []
    text = recall.summary(outcomes, unmatched, len(found))
    row = next(line for line in text.splitlines() if line.strip().startswith("all"))
    # labels 2: exact 0, near 0, partial 0, garbled 1, missed 1 -> strict, +near and +partial stay 0%, +garbled 50%.
    assert row.split() == ["all", "2", "0", "0", "0", "1", "1", "0.0%", "0.0%", "0.0%", "50.0%"]
