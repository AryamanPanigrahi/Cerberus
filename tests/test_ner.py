import re
from pathlib import Path

import pytest

from cerberus import detect as detection
from cerberus.detect import allowlist, ner
from cerberus.extract.ir import BlockType, Container, Document, Source

# All names and addresses below are invented.

NAMES = ("Jane Sample", "Omar Placeholder", "T. Sample", "Ana Maria de la Example")
ADDRESS = "12 Example Street, Springfield, EX 00000"


def fake_predict(texts, labels, threshold):
    """Stands in for the model: "finds" known strings, plus two typical mistakes."""
    assert "organization" in labels and "job title" in labels  # distractors are always offered
    results = []
    for text in texts:
        spans = []
        for value in (*NAMES, ADDRESS):
            label = "address" if value == ADDRESS else "person"
            spans += [dict(start=m.start(), end=m.end(), label=label, score=0.9) for m in re.finditer(re.escape(value), text)]
        for wrong, label in (("Chief Risk Officer", "person"), ("Example Corp", "organization"), ("NAME", "person")):
            spans += [dict(start=m.start(), end=m.end(), label=label, score=0.8) for m in re.finditer(wrong, text)]
        results.append(spans)
    return results


def found(doc: Document):
    return [(f.entity, f.text) for f in ner.detect([doc], fake_predict, "fake")]


# --- precision filters ----------------------------------------------------


@pytest.mark.parametrize("text", ["Jane Sample", "T. Sample", "J. Q. Sample", "Ana Maria de la Example",
                                  "Mary-Ann O'Sample", "Hyun-Woo Sample"])
def test_shapes_accepted_as_names(text):
    assert allowlist.plausible_person(text)


@pytest.mark.parametrize("text", ["Sample", "EXAMPLE CORP", "Chief Risk Officer", "Primary Supplier Contact",
                                  "The Data Protection Officer", "jane sample", "Appendix 4", "Xq Zz yY ww",
                                  "One Two Three Four Five Six"])
def test_shapes_rejected_as_names(text):
    assert not allowlist.plausible_person(text)


@pytest.mark.parametrize("text", [ADDRESS, "Flat 4C, 9 Sample Road", "Somewhere, EX 12345",
                                  "Suite 200, 15 Example Avenue", "ul. Example 5, 00-001 Sampleville"])
def test_shapes_accepted_as_addresses(text):
    assert allowlist.plausible_address(text)


@pytest.mark.parametrize("text", ["Workflow Development", "Appendix 4", "Sample City 24F", "7. Annex C 12",
                                  "Tier 2 Entry"])
def test_shapes_rejected_as_addresses(text):
    assert not allowlist.plausible_address(text)


# --- detector plumbing ----------------------------------------------------


def test_names_and_addresses_in_running_text():
    doc = Document("d0c", "sample.docx", "docx")
    doc.add(BlockType.PARAGRAPH, f"Jane Sample, the Chief Risk Officer of Example Corp, lives at {ADDRESS}.")
    assert found(doc) == [("PERSON", "Jane Sample"), ("ADDRESS", ADDRESS)]


def test_table_row_is_read_as_one_line_and_spans_map_back_to_cells():
    doc = Document("d0c", "sample.pdf", "pdf")
    table = doc.add(BlockType.TABLE, container=Container.TABLE)
    headers, cells = {}, {}
    for r, row in enumerate([("NAME", "MANAGER"), ("Jane Sample", "Omar Placeholder")]):
        for c, text in enumerate(row):
            cell = doc.add(BlockType.TABLE_CELL, text, container=Container.TABLE, source=Source.OCR,
                           parent_id=table.block_id, row=r, col=c, header_id=headers.get(c))
            cells[(r, c)] = cell
            if r == 0:
                headers[c] = cell.block_id

    (row_piece,) = [p for p in ner._pieces(doc) if "Jane" in p.text]
    assert row_piece.text == "NAME: Jane Sample | MANAGER: Omar Placeholder"

    results = ner.detect([doc], fake_predict, "fake")
    assert [(f.block_id, f.start, f.end, f.text) for f in results] == [
        (cells[(1, 0)].block_id, 0, 11, "Jane Sample"),
        (cells[(1, 1)].block_id, 0, 16, "Omar Placeholder"),
    ]
    # "NAME" in the added header text is context, not content: it maps to no cell.
    assert all(f.location == "table" and "ocr" in f.flags for f in results)


def test_hidden_file_property_is_given_its_field_name_as_context():
    doc = Document("d0c", "sample.docx", "docx")
    block = doc.add(BlockType.METADATA, "Jane Sample", native_ref={"core_property": "last_modified_by"})
    (piece,) = ner._pieces(doc)
    assert piece.text == "last modified by: Jane Sample"
    (finding,) = ner.detect([doc], fake_predict, "fake")
    assert (finding.block_id, finding.start, finding.end) == (block.block_id, 0, 11)


def test_long_text_is_split_and_each_name_reported_once_at_the_right_place():
    filler = "word " * 150
    text = f"{filler}Jane Sample {filler}Omar Placeholder {filler}"
    doc = Document("d0c", "sample.docx", "docx")
    block = doc.add(BlockType.PARAGRAPH, text)
    pieces = list(ner._pieces(doc))
    assert len(pieces) > 2 and all(len(p.text.split()) <= 160 for p in pieces)
    results = ner.detect([doc], fake_predict, "fake")
    assert [(f.text, block.text[f.start : f.end]) for f in results] == [
        ("Jane Sample", "Jane Sample"), ("Omar Placeholder", "Omar Placeholder"),
    ]


def test_rules_and_ner_are_combined_and_overlaps_settled(monkeypatch):
    monkeypatch.setattr(ner, "load_predictor", lambda: (fake_predict, "fake"))
    doc = Document("d0c", "sample.docx", "docx")
    doc.add(BlockType.PARAGRAPH, "Jane Sample (EMP-00001) can be reached at j.sample@corp.example.")
    assert [(f.entity, f.detector) for f in detection.detect([doc])] == [
        ("PERSON", "ner"), ("STAFF_ID", "rules"), ("EMAIL", "rules"),
    ]
    assert [f.entity for f in detection.detect([doc], use_ner=False)] == ["STAFF_ID", "EMAIL"]


@pytest.mark.skipif(not Path(ner.MODEL_PATH).is_dir(), reason="the NER model has not been downloaded")
def test_the_real_model_finds_names_and_not_titles():
    doc = Document("d0c", "sample.docx", "docx")
    doc.add(BlockType.PARAGRAPH, "Jane Sample approved the request and passed it to Omar Placeholder, "
                                 "the Chief Risk Officer.")
    doc.add(BlockType.PARAGRAPH, "Primary supplier contact and Email ID of supplier lead are mandatory attributes.")
    people = [f.text for f in ner.detect([doc]) if f.entity == "PERSON"]
    assert people == ["Jane Sample", "Omar Placeholder"]


def test_a_name_the_model_cut_short_is_completed():
    def cut_short(texts, labels, threshold):
        return [[dict(start=t.index("Q. Sample"), end=t.index("Q. Sample") + 9, label="person", score=0.4)]
                if "Q. Sample" in t else [] for t in texts]

    doc = Document("d0c", "sample.docx", "docx")
    doc.add(BlockType.PARAGRAPH, "It was signed by Jane Q. Sample yesterday.")
    doc.add(BlockType.PARAGRAPH, "The Manager Q. Sample signed it.")  # a title is not a first name
    assert [f.text for f in ner.detect([doc], cut_short, "fake")] == ["Jane Q. Sample", "Q. Sample"]


def test_a_span_reaching_across_a_line_break_is_two_names():
    def merged(texts, labels, threshold):
        return [[dict(start=t.index("Jane"), end=t.index("Placeholder") + 11, label="person", score=0.4)]
                if "Jane" in t else [] for t in texts]

    doc = Document("d0c", "sample.docx", "docx")
    doc.add(BlockType.PARAGRAPH, "Jane Sample\nOmar Placeholder")
    assert [f.text for f in ner.detect([doc], merged, "fake")] == ["Jane Sample", "Omar Placeholder"]
