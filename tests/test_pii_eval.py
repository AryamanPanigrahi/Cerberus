from cerberus.detect.finding import make
from cerberus.eval import pii
from cerberus.extract.ir import BlockType, Container, Document
from cerberus.redact import tokens

# All names and identifiers below are invented.


def sample():
    doc = Document("d0c", "sample.pdf", "pdf")
    first = doc.add(BlockType.PARAGRAPH, "Jane Sample approved it on Monday.", page=1)
    cell = doc.add(BlockType.TABLE_CELL, "EMP-00001", container=Container.TABLE, page=2)
    found = [
        make(doc, first, 0, 11, entity="PERSON", detector="ner", rule="m", confidence=0.4, location="prose"),
        make(doc, cell, 0, 9, entity="STAFF_ID", detector="rules", rule="r", confidence=0.9, location="table"),
    ]
    tokenized, _ = tokens.assign(found)
    return doc, tokenized


def test_sheet_has_one_row_per_finding_with_its_context():
    doc, tokenized = sample()
    rows = pii.rows_for(doc, tokenized)
    assert [(r["entity"], r["value"], r["page"], r["location"], r["token"]) for r in rows] == [
        ("PERSON", "Jane Sample", 1, "prose", "<PERSON_1>"),
        ("STAFF_ID", "EMP-00001", 2, "table", "<STAFF_ID_1>"),
    ]
    assert rows[0]["context"] == "[[Jane Sample]] approved it on Monday."
    assert all(r["verdict"] == "" for r in rows)


def test_export_keeps_verdicts_and_promotes_a_miss_that_is_now_found(tmp_path):
    doc, tokenized = sample()
    (path,) = pii.export([doc], tokenized[:1], tmp_path)  # first run: only the name was found
    rows = pii.read(path)
    rows[0]["verdict"] = "ok"
    rows.append({"verdict": "missed", "entity": "STAFF_ID", "value": "emp-00001", "page": "2", "location": "table"})
    rows.append({"verdict": "missed", "entity": "PHONE", "value": "+1 555 010 0101", "page": "1", "location": "prose"})
    pii.write(path, rows)

    pii.export([doc], tokenized, tmp_path)  # the pipeline improved and now finds the ID
    again = pii.read(path)
    assert [(r["verdict"], r["entity"], r["value"]) for r in again] == [
        ("ok", "PERSON", "Jane Sample"),
        ("ok", "STAFF_ID", "EMP-00001"),
        ("missed", "PHONE", "+1 555 010 0101"),
    ]


def test_precision_and_recall_from_verdicts():
    rows = (
        [{"verdict": "ok", "entity": "PERSON", "location": "prose"}] * 8
        + [{"verdict": "wrong", "entity": "PERSON", "location": "prose"}] * 2
        + [{"verdict": "missed", "entity": "PERSON", "location": "image"}] * 2
        + [{"verdict": "ok", "entity": "EMAIL", "location": "table"}] * 5
        + [{"verdict": "", "entity": "EMAIL", "location": "table"}] * 3
    )
    result = pii.score(rows)
    overall = result["all"]["all"]
    assert (overall.ok, overall.wrong, overall.missed, overall.unchecked) == (13, 2, 2, 3)
    assert round(overall.precision, 3) == round(13 / 15, 3)
    assert round(overall.recall, 3) == round(13 / 15, 3)
    person = result["entity"]["PERSON"]
    assert (person.precision, person.recall) == (0.8, 0.8)
    assert result["entity"]["EMAIL"].precision == 1.0
    # The recall gap between pictures and ordinary text is read straight off the split.
    assert result["location"]["image"].recall == 0.0 and result["location"]["prose"].recall == 1.0


def test_nothing_checked_gives_no_score():
    result = pii.score([{"verdict": "", "entity": "PERSON", "location": "prose"}])
    assert result["all"]["all"].precision is None and result["all"]["all"].recall is None
