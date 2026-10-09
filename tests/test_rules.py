import pytest

from cerberus.detect import rules
from cerberus.detect.rules import validators
from cerberus.extract.ir import BlockType, Container, Document, Source

# Every identifier below is invented or a published test value (the 900 SSN
# range, the 4111... test card number).


def doc_of(*texts: str, source: Source = Source.NATIVE) -> Document:
    doc = Document("d0c", "sample.docx", "docx")
    for text in texts:
        doc.add(BlockType.PARAGRAPH, text, source=source)
    return doc


def found(*texts: str, source: Source = Source.NATIVE):
    return [(f.entity, f.text) for f in rules.detect([doc_of(*texts, source=source)])]


# --- validators -----------------------------------------------------------


def test_luhn():
    assert validators.luhn("4111111111111111")
    assert not validators.luhn("4111111111111112")


def test_pesel_needs_a_real_date_and_a_matching_check_digit():
    assert validators.pesel("90010100009")
    assert not validators.pesel("90010100008")  # wrong check digit
    assert not validators.pesel("90133100009")  # month 13
    assert validators.pesel_birth_date("012101").year == 2001  # month + 20 means the 2000s


def test_pan_fourth_letter_is_the_holder_type():
    assert validators.pan_holder("ABCPE1234F") == "individual"
    assert validators.pan_holder("ABCXE1234F") is None


def test_ssn_structure_accepts_and_flags_the_test_range():
    assert validators.ssn_flags("123", "45", "6789") == ()
    assert validators.ssn_flags("900", "12", "3456") == ("test_range",)
    assert validators.ssn_flags("000", "12", "3456") is None
    assert validators.ssn_flags("666", "12", "3456") is None
    assert validators.ssn_flags("123", "00", "6789") is None


# --- rules ----------------------------------------------------------------


def test_contact_details():
    assert found("Write to j.sample@corp.example or call +1 (555) 010-0101.") == [
        ("EMAIL", "j.sample@corp.example"), ("PHONE", "+1 (555) 010-0101"),
    ]
    assert found("Desk +44 20 5550 0163, mobile +91 98450 55011") == [
        ("PHONE", "+44 20 5550 0163"), ("PHONE", "+91 98450 55011"),
    ]
    # A phone number does not swallow what follows it.
    assert found("Call +1 (555) 010-0177 (24 hours).") == [("PHONE", "+1 (555) 010-0177")]


def test_ssn_is_reported_with_its_flags_and_partial_form():
    (ssn,) = rules.detect([doc_of("Her SSN is 900-12-3456 on file.")])
    assert (ssn.entity, ssn.text, ssn.confidence) == ("SSN", "900-12-3456", 0.9)
    assert set(ssn.flags) == {"keyword", "test_range"}
    assert found("Reference 000-12-3456 was closed.") == []  # impossible area number

    (partial,) = rules.detect([doc_of("Confirm the last four digits of your SSN (3456) to proceed.")])
    assert (partial.entity, partial.text, partial.subtype) == ("SSN", "3456", "partial")


def test_same_shape_with_a_tax_keyword_is_a_tax_id():
    assert found("TIN 900-70-1234 was supplied.") == [("TAX_ID", "900-70-1234")]
    assert found("Employer EIN: 12-3456789") == [("TAX_ID", "12-3456789")]
    assert found("Order 12-3456789 shipped.") == []


def test_card_needs_more_than_a_passing_checksum():
    assert found("Card number 4111 1111 1111 1111 was charged.") == [("CARD", "4111 1111 1111 1111")]
    assert found("Card number 4111 1111 1111 1112 was charged.") == []  # fails Luhn
    assert found("Tracking id 4111111111111111 dispatched.") == []  # passes Luhn, nothing says card
    assert found("the card ending 4242 was declined") == [("CARD", "4242")]


def test_pan_pesel_passport_and_birth_date():
    assert found("PAN ABCPE1234F") == [("PAN", "ABCPE1234F")]
    assert found("Token ABCXE1234F") == []  # fourth letter is not a holder type
    assert found("PESEL 90010100009") == [("PESEL", "90010100009")]
    assert found("Serial 90010100008") == []
    assert found("Passport no. X1234567 expires soon") == [("PASSPORT", "X1234567")]
    assert found("born 3 August 1996 in Springfield") == [("DOB", "3 August 1996")]
    assert found("The review is due 3 August 2026.") == []  # a date nobody calls a birth date


def test_already_masked_pesel_is_still_reported():
    (masked,) = rules.detect([doc_of("PESEL 900101xxxxx")])
    assert (masked.entity, masked.subtype, masked.text) == ("PESEL", "masked", "900101xxxxx")
    assert found("PESEL 901399xxxxx") == []  # the visible digits are not a date


def test_ocr_text_gets_tolerant_patterns_and_typed_text_does_not():
    noisy = "Contact |.sample@corp.example or n.other @corp. example, SSN 900-12.3456, EMP- 12345"
    assert found(noisy, source=Source.OCR) == [
        ("EMAIL", "|.sample@corp.example"), ("EMAIL", "n.other @corp. example"),
        ("SSN", "900-12.3456"), ("STAFF_ID", "EMP- 12345"),
    ]
    assert ("SSN", "900-12.3456") not in found(noisy)
    # The tolerant e-mail pattern stops at the end of the address.
    assert found("Mail j.sample@corp.example. For help, ask.", source=Source.OCR) == [
        ("EMAIL", "j.sample@corp.example"),
    ]


# --- staff identifiers ----------------------------------------------------


def roster() -> Document:
    doc = Document("d0c", "sample.pdf", "pdf")
    table = doc.add(BlockType.TABLE, container=Container.TABLE)
    headers = {}
    for r, row in enumerate([("NAME", "VENDOR ID"), ("Jane Sample", "VND-IN-0001"),
                             ("Omar Placeholder", "VND-PL-0002"), ("Ana Example", "VND-IN-0003")]):
        for c, text in enumerate(row):
            cell = doc.add(BlockType.TABLE_CELL, text, container=Container.TABLE,
                           parent_id=table.block_id, row=r, col=c, header_id=headers.get(c))
            if r == 0:
                headers[c] = cell.block_id
    doc.add(BlockType.PARAGRAPH, "Risk REF-4488 was raised after VND-PL-0002 kept access for 34 days.")
    doc.add(BlockType.PARAGRAPH, "Lee Specimen (DRT-0042) and Kim Dummy (DRT-0043) and Pat Proxy (DRT-0044) approved.")
    return doc


def test_staff_prefixes_are_learned_from_labels_and_names_then_applied_everywhere():
    doc = roster()
    assert rules.learn_staff_prefixes([doc]) == {"EMP", "EMPL", "EID", "STAFF", "VND", "DRT"}
    ids = [f for f in rules.detect([doc]) if f.entity == "STAFF_ID"]
    assert [f.text for f in ids] == [
        "VND-IN-0001", "VND-PL-0002", "VND-IN-0003", "VND-PL-0002", "DRT-0042", "DRT-0043", "DRT-0044",
    ]
    # The risk reference has the same shape but was never seen identifying a person.
    assert "REF-4488" not in [f.text for f in rules.detect([doc])]
    # In the table the column header vouches for it; in the sentence nothing does.
    assert [f.confidence for f in ids[:4]] == [0.9, 0.9, 0.9, 0.8]


def test_prefixes_learned_in_one_file_apply_to_another():
    other = doc_of("Access was revoked for VND-IN-0009 on Monday.")
    assert found("Access was revoked for VND-IN-0009 on Monday.") == []
    both = rules.detect([roster(), other])
    assert ("STAFF_ID", "VND-IN-0009") in [(f.entity, f.text) for f in both]


# --- location and traceability -------------------------------------------


def test_location_separates_fields_tables_prose_and_pictures():
    doc = Document("d0c", "sample.pdf", "pdf")
    doc.add(BlockType.PARAGRAPH, "SSN: 900-12-3456")
    doc.add(BlockType.PARAGRAPH, "On Monday the administrator shared a folder that listed, among "
                                 "other details, an SSN of 900-12-3457 for one member of staff.")
    table = doc.add(BlockType.TABLE, container=Container.TABLE)
    doc.add(BlockType.TABLE_CELL, "900-12-3458", container=Container.TABLE, parent_id=table.block_id, row=0, col=0)
    doc.add(BlockType.PARAGRAPH, "SSN 900-12-3459", container=Container.IMAGE, source=Source.OCR, quarantined=True)
    results = rules.detect([doc])
    assert [f.location for f in results] == ["body", "prose", "table", "image"]
    assert "quarantined" in results[3].flags and "ocr" in results[3].flags


def test_a_finding_carries_everything_needed_to_trace_it():
    doc = doc_of("Reach j.sample@corp.example today.")
    (finding,) = rules.detect([doc])
    block = doc.blocks[0]
    assert (finding.doc_id, finding.block_id) == (doc.doc_id, block.block_id)
    assert block.text[finding.start : finding.end] == finding.text == "j.sample@corp.example"
    assert (finding.detector, finding.rule) == ("rules", "email")


def test_overlapping_matches_resolve_to_the_surest():
    # Sixteen grouped digits also contain phone-shaped runs; only the card is reported.
    assert found("Card 4111-1111-1111-1111") == [("CARD", "4111-1111-1111-1111")]


@pytest.mark.parametrize("text", [
    "Version 2.0 of DOC-STD-001 applies from 30/06/2030.",
    "Ticket TKT-2030-0001 and control CTL-ABC-01 are referenced in section 9.9.",
    "Primary supplier contact and Email ID of supplier lead are mandatory attributes.",
    "The limit is USD 100,000 for 12 months; 1,234 items were delayed.",
])
def test_lookalikes_are_left_alone(text):
    assert found(text) == []


# --- what OCR does to values inside pictures -------------------------------


def picture_lines(*lines, engine="doctr"):
    """Lines read inside one picture, top to bottom: (text, x0, y0) each, 12 high."""
    from cerberus.extract.ir import BBox, Unit

    doc = Document("d0c", "scan.pdf", "pdf")
    doc.add(BlockType.PARAGRAPH, "Write to j.sample@corp.example or o.placeholder@corp.example about the scales.")
    region = doc.add(BlockType.IMAGE_REGION, container=Container.IMAGE, page=1)
    for text, x, y in lines:
        doc.add(BlockType.PARAGRAPH, text, container=Container.IMAGE, source=Source.OCR, page=1,
                parent_id=region.block_id, bbox=BBox(x, y, x + 6 * len(text), y + 12, Unit.PT),
                native_ref={"engine": engine})
    return doc


def in_picture(*lines):
    return [(f.entity, f.text, f.rule) for f in rules.detect([picture_lines(*lines)]) if f.location == "image"]


def test_a_pan_run_into_its_label_is_still_found():
    assert in_picture(("Note PANABCPS1234D", 0, 0)) == [("PAN", "ABCPS1234D", "pan_structure")]
    assert in_picture(("Note PANABCXS1234D", 0, 0)) == []  # the holder letter is still checked


def test_initials_and_a_surname_in_a_picture_are_reported_as_a_name():
    for text in ("Owner J. Q. Sample", "Owner JQ. Sample", "Owner J. Q Sample", "Owner J.Q.Sample"):
        ((entity, value, rule),) = in_picture((text, 0, 0))
        assert (entity, rule) == ("PERSON", "initials_and_surname") and value.endswith("Sample") and value[0] == "J"
    assert in_picture(("Chief Risk Officer", 0, 0)) == []
    # "scales" is a word this document writes in small letters: a heading, not a surname.
    assert in_picture(("Appendix A. Scales", 0, 0)) == []
    # Outside a picture the form is left to the language model.
    assert found("Owner J. Q. Sample signed.") == []


def test_an_address_whose_at_sign_was_misread_is_found_by_its_domain():
    assert in_picture(("auth user=j.sampleecorp.example ok", 0, 0)) == [("EMAIL", "j.sampleecorp.example", "email_at_misread")]
    assert in_picture(("mail o.placeholderacorp.exarnple sent", 0, 0)) == [("EMAIL", "o.placeholderacorp.exarnple", "email_at_misread")]
    assert in_picture(("user=j.sample�corp.example src", 0, 0)) == [("EMAIL", "j.sample�corp.example", "email_at_misread")]
    # A web address, a domain these documents never use, and a word that merely ends alike are left alone.
    assert in_picture(("see www.corp.example today", 0, 0)) == []
    assert in_picture(("j.sampleeother.example", 0, 0)) == []


def test_a_staff_id_after_a_misread_equals_sign_is_found():
    assert in_picture(("owner -EMP.00001 sev-high", 0, 0)) == [("STAFF_ID", "EMP.00001", "staff_id_prefix")]


def test_a_card_number_already_masked_in_the_source_is_reported_as_partial():
    (finding,) = rules.detect([doc_of("Matched card 4111-XXXX-XXXX-1111 in the file.")])
    assert (finding.entity, finding.text, finding.subtype, finding.rule) == ("CARD", "4111-XXXX-XXXX-1111", "masked", "card_already_masked")
    assert found("Order 4111-ABCD-EFGH-1111 shipped.") == []
    assert in_picture(("match-4111-xxxx xxxx-1111 2026", 0, 0)) == [("CARD", "4111-xxxx xxxx-1111", "card_already_masked")]


def test_a_label_on_the_line_above_supports_the_value_below_it():
    assert in_picture(("DATE OF BIRTH", 100, 0), ("28 February 1979", 100, 16)) == [("DOB", "28 February 1979", "date_with_birth_keyword")]
    # Not when the line above is far away, off to one side, or a sentence.
    assert in_picture(("DATE OF BIRTH", 100, 0), ("28 February 1979", 100, 90)) == []
    assert in_picture(("DATE OF BIRTH", 100, 0), ("28 February 1979", 400, 16)) == []
    assert in_picture(("The date of birth of each member is recorded", 100, 0), ("28 February 1979", 100, 16)) == []
    assert in_picture(("DOB 01/02/1980", 100, 0), ("Left 28/02/2026", 100, 16)) == [("DOB", "01/02/1980", "date_with_birth_keyword")]
