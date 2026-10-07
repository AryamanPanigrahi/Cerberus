from cerberus.detect.finding import make
from cerberus.extract.ir import BlockType, Document
from cerberus.redact import tokens

# All names and identifiers below are invented.


def findings(*items, doc_id="d0c"):
    """(entity, text) or (entity, text, location) -> findings, one block each, in order."""
    doc = Document(doc_id, "sample.docx", "docx")
    out = []
    for entity, text, *rest in items:
        block = doc.add(BlockType.PARAGRAPH, text)
        location = rest[0] if rest else "body"
        out.append(make(doc, block, 0, len(text), entity=entity, detector="test", rule="t", confidence=0.9,
                        location="body" if location == "ocr" else location,
                        flags=("ocr",) if location == "ocr" else ()))
    return out


def tokens_of(*items):
    tokenized, _ = tokens.assign(findings(*items))
    return [t.token for t in tokenized]


def test_written_forms_of_one_name_share_a_token():
    assert tokens_of(
        ("PERSON", "Jane Q. Sample"), ("PERSON", "Jane Sample"), ("PERSON", "J. Sample"),
        ("PERSON", "J.Q. Sample"), ("PERSON", "JANE SAMPLE"), ("PERSON", "Dr Jane Sample"),
        ("PERSON", "Omar Placeholder"),
    ) == ["<PERSON_1>"] * 6 + ["<PERSON_2>"]


def test_a_shared_surname_does_not_link_two_people():
    assert tokens_of(("PERSON", "Jane Sample"), ("PERSON", "Omar Sample"), ("PERSON", "Ana Sample-Example")) == [
        "<PERSON_1>", "<PERSON_2>", "<PERSON_3>",
    ]


def test_an_abbreviation_that_fits_two_people_is_kept_apart():
    result = tokens_of(("PERSON", "Jane Sample"), ("PERSON", "John Sample"), ("PERSON", "J. Sample"),
                       ("PERSON", "O. Placeholder"))
    assert result[0] != result[1]
    assert result[2] not in result[:2]  # "J. Sample" could be either: neither is guessed
    assert result[3] not in result[:3]  # nobody known fits: its own token


def test_different_middle_initials_are_different_people():
    a, b, c = tokens_of(("PERSON", "Jane Q. Sample"), ("PERSON", "Jane R. Sample"), ("PERSON", "Jane Sample"))
    assert a != b and c not in (a, b)


def test_a_scanned_misreading_resolves_to_the_known_person():
    assert tokens_of(("PERSON", "Jane Sampleton"), ("PERSON", "Jane Sampieton", "ocr")) == ["<PERSON_1>"] * 2
    # In typed text a one-letter difference is a different person.
    assert tokens_of(("PERSON", "Jane Sampleton"), ("PERSON", "Jane Sampieton")) == ["<PERSON_1>", "<PERSON_2>"]


def test_a_single_word_is_never_linked_to_a_person():
    full, single = tokens_of(("PERSON", "Jane Sample"), ("PERSON", "Ms Sample"))
    assert full != single


def test_values_are_compared_after_normalising_how_they_are_written():
    assert tokens_of(
        ("EMAIL", "J.Sample@Corp.Example"), ("EMAIL", "j.sample@corp.example"), ("EMAIL", "|.other@corp.example"),
        ("EMAIL", "l.other@corp.example"),
    ) == ["<EMAIL_1>", "<EMAIL_1>", "<EMAIL_2>", "<EMAIL_2>"]
    assert tokens_of(("PHONE", "+1 (555) 010-0101"), ("PHONE", "+1 555 010 0101"), ("PHONE", "+1 555 010 0102")) == [
        "<PHONE_1>", "<PHONE_1>", "<PHONE_2>",
    ]
    assert tokens_of(("STAFF_ID", "EMP-00001"), ("STAFF_ID", "EMP- 00001"), ("STAFF_ID", "EMP.00001")) == ["<STAFF_ID_1>"] * 3
    assert tokens_of(("SSN", "900-12-3456"), ("SSN", "900 12 3456")) == ["<SSN_1>", "<SSN_1>"]


def test_each_entity_has_its_own_numbering_and_tokens_are_traceable():
    tokenized, vault = tokens.assign(findings(("PERSON", "Jane Sample"), ("EMAIL", "j.sample@corp.example"),
                                              ("PERSON", "J. Sample")))
    assert [t.token for t in tokenized] == ["<PERSON_1>", "<EMAIL_1>", "<PERSON_1>"]
    assert vault.values["<PERSON_1>"] == ["Jane Sample", "J. Sample"]


def test_the_same_person_in_two_files_gets_one_token():
    both = findings(("PERSON", "Jane Sample"), doc_id="aaa") + findings(("PERSON", "Jane Sample"), doc_id="bbb")
    tokenized, _ = tokens.assign(both)
    assert {t.finding.doc_id for t in tokenized} == {"aaa", "bbb"}
    assert {t.token for t in tokenized} == {"<PERSON_1>"}


def test_a_saved_vault_keeps_tokens_stable_across_runs(tmp_path):
    path = tmp_path / "vault.json"
    _, vault = tokens.assign(findings(("PERSON", "Jane Sample"), ("PERSON", "Omar Placeholder")))
    vault.save(path)
    # A later run sees a new person first; the earlier two keep their numbers.
    later, _ = tokens.assign(findings(("PERSON", "Ana Example"), ("PERSON", "Omar Placeholder"),
                                      ("PERSON", "Jane Sample")), tokens.Vault.load(path))
    assert [t.token for t in later] == ["<PERSON_3>", "<PERSON_2>", "<PERSON_1>"]
    assert tokens.Vault.load(tmp_path / "missing.json").tokens == {}


def test_identifiers_seen_only_inside_pictures_are_listed():
    tokenized, _ = tokens.assign(findings(
        ("STAFF_ID", "EMP-00001", "image"), ("STAFF_ID", "EMP-00002", "image"), ("STAFF_ID", "EMP-00002", "table"),
        ("PERSON", "Jane Sample", "image"), ("PERSON", "Jane Sample", "image"),
    ))
    assert tokens.image_only(tokenized) == ["<PERSON_1>", "<STAFF_ID_1>"]


def test_glued_initials_are_initials_not_a_first_name():
    # Scanned text often reads "J. Q." as "JQ"; it must still resolve to the one person it fits.
    assert tokens_of(("PERSON", "Jane Q. Sample"), ("PERSON", "JQ Sample", "ocr"), ("PERSON", "J. Q. Sample")) == [
        "<PERSON_1>"] * 3
