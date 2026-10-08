from cerberus.detect import dictionary
from cerberus.detect.finding import make
from cerberus.extract.ir import BBox, BlockType, Container, Document, Source, Unit, Word

# All names below are invented.


def seeded(*names: str, location: str = "prose", flags: tuple = (), times: int = 2):
    """Findings as the model would have produced them, to seed the dictionary.

    Each name is seen twice by default, which is enough evidence to sweep for it.
    """
    doc = Document("seed", "seed.docx", "docx")
    out = []
    for name in names:
        for _ in range(times):
            block = doc.add(BlockType.PARAGRAPH, name)
            out.append(make(doc, block, 0, len(name), entity="PERSON", detector="ner", rule="fake",
                            confidence=0.4, flags=flags, location=location))
    return out


def sweep(text: str, *names: str, source: Source = Source.NATIVE):
    doc = Document("d0c", "sample.pdf", "pdf")
    doc.add(BlockType.PARAGRAPH, text, source=source)
    return [(f.text, f.rule) for f in dictionary.detect([doc], seeded(*names))]


def test_written_forms_of_a_name():
    assert dictionary.name_forms("Jane Q. Sample") == {
        "Jane Q. Sample": "name_exact",
        "Jane Sample": "name_without_middle",
        "Sample, Jane": "name_reversed",
        "J. Sample": "name_initial",
        "J. Q. Sample": "name_initial",
    }
    # Nothing is derived from a name that is already abbreviated, and a title is not a first name.
    assert dictionary.name_forms("J. Sample") == {"J. Sample": "name_exact"}
    assert dictionary.name_forms("Dr Jane Sample")["J. Sample"] == "name_initial"
    assert dictionary.name_forms("Ms Sample") == {"Sample": "name_exact"}


def test_every_mention_of_a_known_name_is_found():
    text = ("Jane Q. Sample approved it. Later J. Sample and J.Q. Sample signed, "
            "JANE SAMPLE was printed on the badge, and the directory lists Sample, Jane.")
    assert sweep(text, "Jane Q. Sample") == [
        ("Jane Q. Sample", "name_exact"), ("J. Sample", "name_initial"), ("J.Q. Sample", "name_initial"),
        ("JANE SAMPLE", "name_without_middle"), ("Sample, Jane", "name_reversed"),
    ]


def test_a_surname_or_first_name_alone_is_not_swept():
    assert sweep("Sample sizes were small, and Jane agreed.", "Jane Sample") == []
    # ...so a different person with the same surname is not swept up either.
    assert sweep("Omar Sample disagreed.", "Jane Sample") == []


def test_initial_forms_keep_their_case():
    # "A. Park" as a form must not match ordinary words.
    assert sweep("We met in a park and a. parking lot.", "Ana Park") == []
    assert sweep("Signed A. Park.", "Ana Park") == [("A. Park", "name_initial")]


def test_ocr_text_is_matched_approximately_and_typed_text_is_not():
    noisy = "Approved by Jane Sampie and JaneQ. Sample on Monday."
    assert sweep(noisy, "Jane Q. Sample", source=Source.OCR) == [
        ("Jane Sampie", "name_approximate"), ("JaneQ. Sample", "name_approximate"),
    ]
    assert sweep(noisy, "Jane Q. Sample") == []


def test_names_read_from_pictures_do_not_seed_the_dictionary():
    assert dictionary.candidates([], seeded("Zorp Blenk", location="image")) == {}
    assert dictionary.candidates([], seeded("Jane Sample", flags=("quarantined",))) == {}
    assert dictionary.candidates([], seeded("Jane Sample")) == {"Jane Sample": ("repeated",)}


def test_a_name_found_in_one_file_is_swept_in_another_and_in_tables():
    other = Document("0ther", "deck.pptx", "pptx")
    table = other.add(BlockType.TABLE, container=Container.TABLE)
    cell = other.add(BlockType.TABLE_CELL, "J. Sample\nCIO", container=Container.TABLE, parent_id=table.block_id, row=0, col=0)
    (finding,) = dictionary.detect([other], seeded("Jane Sample"))
    assert (finding.doc_id, finding.block_id, finding.text) == ("0ther", cell.block_id, "J. Sample")
    assert (finding.detector, finding.location, finding.confidence) == ("dictionary", "table", 0.85)


def test_ocr_footers_do_not_seed_the_dictionary():
    doc = Document("d0c", "scan.pdf", "pdf")
    footer = doc.add(BlockType.FOOTER, "Zu Qnregistered when printed", source=Source.OCR, page=1)
    doc.add(BlockType.FOOTER, "Zu registered when printed", source=Source.OCR, page=2)
    junk = make(doc, footer, 0, 15, entity="PERSON", detector="ner", rule="fake", confidence=0.3, location="body")
    assert dictionary.detect([doc], [junk, junk]) == []


# --- the evidence gate ----------------------------------------------------


def person(doc, block, start, end):
    return make(doc, block, start, end, entity="PERSON", detector="ner", rule="fake", confidence=0.3, location="prose")


def other(doc, block, start, end, entity):
    return make(doc, block, start, end, entity=entity, detector="rules", rule="fake", confidence=0.9, location="prose")


def test_a_name_seen_once_with_nothing_to_back_it_is_not_swept():
    doc = Document("d0c", "sample.docx", "docx")
    first = doc.add(BlockType.PARAGRAPH, "Zorp Blenk was selected.")
    doc.add(BlockType.PARAGRAPH, "Zorp Blenk appears again here.")
    doc.add(BlockType.PARAGRAPH, "Z. Blenk and Zorp Blemk are near it.", source=Source.OCR)
    lone = person(doc, first, 0, 10)
    assert dictionary.candidates([doc], [lone]) == {"Zorp Blenk": ()}
    # Exact repeats are still caught; its abbreviated and approximate forms are not searched for.
    assert [(f.text, f.rule) for f in dictionary.detect([doc], [lone])] == [
        ("Zorp Blenk", "name_exact"), ("Zorp Blenk", "name_exact"),
    ]


def test_each_kind_of_evidence_lets_a_name_be_swept():
    doc = Document("d0c", "sample.pdf", "pdf")
    mail = doc.add(BlockType.PARAGRAPH, "Jane Sample can be reached at j.sample@corp.example.")
    same = doc.add(BlockType.PARAGRAPH, "Omar Placeholder (EMP-00001) approved.")
    table = doc.add(BlockType.TABLE, container=Container.TABLE)
    cells = dict(container=Container.TABLE, parent_id=table.block_id)
    head = doc.add(BlockType.TABLE_CELL, "ACCOUNT MANAGER", row=0, col=0, **cells)
    cell = doc.add(BlockType.TABLE_CELL, "Ana Example", row=1, col=0, header_id=head.block_id, **cells)
    row_name = doc.add(BlockType.TABLE_CELL, "Lee Specimen", row=2, col=1, **cells)
    row_id = doc.add(BlockType.TABLE_CELL, "EMP-00002", row=2, col=2, **cells)
    prop = doc.add(BlockType.METADATA, "Kim Dummy", native_ref={"core_property": "author"})
    findings = [
        person(doc, mail, 0, 11), other(doc, mail, 30, 51, "EMAIL"),
        person(doc, same, 0, 16), other(doc, same, 18, 27, "STAFF_ID"),
        person(doc, cell, 0, 11), person(doc, row_name, 0, 12), other(doc, row_id, 0, 9, "STAFF_ID"),
        person(doc, prop, 0, 9),
    ]
    assert dictionary.candidates([doc], findings) == {
        "Jane Sample": ("email",),
        "Omar Placeholder": ("staff_id",),
        "Ana Example": ("header",),
        "Lee Specimen": ("staff_id",),
        "Kim Dummy": ("file_property",),
    }


def test_a_name_on_a_word_the_ocr_was_unsure_of_cannot_seed():
    doc = Document("d0c", "scan.pdf", "pdf")
    box = BBox(0, 0, 1, 1, Unit.PT)
    unsure = doc.add(BlockType.PARAGRAPH, "Qua Vex", source=Source.OCR,
                     words=[Word(0, 3, box, 0.9), Word(4, 7, box, 0.4)])
    sure = doc.add(BlockType.PARAGRAPH, "Jane Sample", source=Source.OCR,
                   words=[Word(0, 4, box, 0.9), Word(5, 11, box, 0.9)])
    findings = [person(doc, unsure, 0, 7), person(doc, unsure, 0, 7), person(doc, sure, 0, 11), person(doc, sure, 0, 11)]
    assert dictionary.candidates([doc], findings) == {"Jane Sample": ("repeated",)}


def test_review_file_round_trip_and_overrides(tmp_path):
    doc = Document("d0c", "sample.docx", "docx")
    a = doc.add(BlockType.PARAGRAPH, "Zorp Blenk was selected.")
    doc.add(BlockType.PARAGRAPH, "Zorp Blenk appears again, and so does Jane Sample.")
    b = doc.add(BlockType.PARAGRAPH, "Jane Sample approved.")
    known = [person(doc, a, 0, 10), person(doc, b, 0, 11), person(doc, b, 0, 11)]

    path = tmp_path / "review" / "names.txt"
    dictionary.write_review(path, dictionary.candidates([doc], known))
    lines = [ln.split("\t") for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("#")]
    assert lines == [["exact", "Zorp Blenk", "no supporting evidence"], ["keep", "Jane Sample", "repeated"]]

    # Unedited, the file changes nothing.
    keep, drop = dictionary.read_review(path)
    unedited = [f.text for f in dictionary.detect([doc], known, keep, drop)]
    assert unedited == [f.text for f in dictionary.detect([doc], known)]

    # A person can overrule the pipeline in either direction.
    path.write_text("keep\tZorp Blenk\ndrop\tJane Sample\n", encoding="utf-8")
    keep, drop = dictionary.read_review(path)
    assert [f.text for f in dictionary.detect([doc], known, keep, drop)] == ["Zorp Blenk", "Zorp Blenk"]
    assert (keep, drop) == (frozenset({"zorp blenk"}), frozenset({"jane sample"}))


def test_a_misread_first_letter_does_not_hide_the_name():
    # OCR read the capital "I" of "Iris" as a small "l"; a shorter piece of the name was also matched.
    text = "Reviewed by lris Q. Sample on Monday."
    found = sweep(text, "Iris Q. Sample", "Q. Sample", source=Source.OCR)
    assert found == [("lris Q. Sample", "name_approximate")]


def test_a_name_is_not_matched_across_a_line_break():
    # Two entries in one table cell: "Jane" ends one line and "Sample" starts the next.
    assert sweep("Mary Jane\nSample Ltd", "Jane Sample") == []
    assert sweep("Jane   Sample", "Jane Sample") == [("Jane   Sample", "name_exact")]
