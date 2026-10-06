from cerberus.extract import layout
from cerberus.extract.ocr import OcrWord

# Synthetic OCR output: every character is 10 px wide, text is 20 px tall,
# lines of a paragraph are 36 px apart. All names and values are invented.

CHAR, HEIGHT = 10, 20


def line(text_by_x: dict[int, str], y: int, key: int, height: int = HEIGHT) -> list[OcrWord]:
    """Words for one line; each entry is a run of text starting at pixel x."""
    words = []
    for x, text in text_by_x.items():
        for token in text.split():
            width = CHAR * len(token) * height // HEIGHT
            words.append(OcrWord(token, x, y, x + width, y + height, 0.95, (1, 1, key)))
            x += width + CHAR * height // HEIGHT
    return words


def page() -> list[OcrWord]:
    rows = [
        ({100: "3. Vendor contacts"}, 100, 32),  # large type: a top-level heading
        ({100: "3.1 Named staff"}, 180, 20),
        ({100: "Each provider supplies a named roster every quarter and the"}, 240, 20),
        ({100: "roster is itself personal data."}, 276, 20),
        ({100: "1. Collect the roster."}, 340, 20),
        ({100: "2. Check it against the register."}, 376, 20),
        ({110: "NAME", 400: "VENDOR ID", 650: "LOCATION"}, 460, 20),
        ({110: "Jane Sample", 400: "VND-00000", 650: "Springfield 09:00-"}, 512, 20),
        ({650: "17:00"}, 548, 20),  # the location cell wraps
        ({110: "Omar Placeholder", 400: "VND-00001", 650: "Shelbyville"}, 600, 20),
        ({100: "Rosters are reviewed by the owner named above before any access"}, 700, 20),
        ({100: "is granted."}, 736, 20),
    ]
    words = []
    for key, (text_by_x, y, height) in enumerate(rows):
        words += line(text_by_x, y, key, height)
    return words


def regions():
    return layout.analyse(layout.build_lines(page()))


def text_of(region) -> str:
    return " ".join(ln.text for ln in region.lines)


def test_headings_lists_and_paragraphs():
    found = [(r.kind, r.level, text_of(r)) for r in regions() if isinstance(r, layout.TextRegion)]
    assert found == [
        ("heading", 1, "3. Vendor contacts"),
        ("heading", 2, "3.1 Named staff"),
        ("paragraph", None, "Each provider supplies a named roster every quarter and the "
                            "roster is itself personal data."),
        ("list_item", None, "1. Collect the roster."),
        ("list_item", None, "2. Check it against the register."),
        ("paragraph", None, "Rosters are reviewed by the owner named above before any access "
                            "is granted."),
    ]


def test_table_columns_rows_and_wrapped_cell():
    (table,) = [r for r in regions() if isinstance(r, layout.TableRegion)]
    cells = [[" ".join(w.text for w in cell) for cell in row] for row in table.cells]
    assert cells == [
        ["NAME", "VENDOR ID", "LOCATION"],
        ["Jane Sample", "VND-00000", "Springfield 09:00- 17:00"],
        ["Omar Placeholder", "VND-00001", "Shelbyville"],
    ]
    assert len(table.columns) == 3 and len(table.rows) == 3


def test_table_sits_between_the_text_around_it():
    kinds = [r.kind if isinstance(r, layout.TextRegion) else "table" for r in regions()]
    assert kinds == ["heading", "heading", "paragraph", "list_item", "list_item", "table", "paragraph"]


def test_lines_split_by_the_engine_are_rejoined():
    words = line({100: "Jane Sample"}, 100, 1) + line({400: "VND-00000"}, 101, 2)
    (only,) = layout.build_lines(words)
    assert only.text == "Jane Sample VND-00000"


def test_small_print_does_not_turn_body_text_into_headings():
    # A figure's small print: more lines than the body, though fewer characters.
    words = []
    for i in range(6):
        words += line({100: "Ordinary body text at the usual size for this page"}, 100 + 36 * i, i)
    for i in range(9):
        words += line({100: "Tiny Figure Caption"}, 400 + 12 * i, 10 + i, height=8)
    kinds = {r.kind for r in layout.analyse(layout.build_lines(words))}
    assert "heading" not in kinds


def table_of(words, rules=()):
    (table,) = [r for r in layout.analyse(layout.build_lines(words), rules) if isinstance(r, layout.TableRegion)]
    return [[" ".join(w.text for w in cell) for cell in row] for row in table.cells]


def test_ruled_lines_decide_rows_when_every_column_wraps():
    rows = [
        ({110: "RATING", 400: "MEANING"}, 460),
        ({110: "Severe and", 400: "Sustained outage of an"}, 512),
        ({110: "widespread", 400: "important service"}, 548),
        ({110: "Minor and", 400: "Localised disruption"}, 600),
        ({110: "contained", 400: "absorbed in a day"}, 636),
    ]
    words = [w for key, (text_by_x, y) in enumerate(rows) for w in line(text_by_x, y, key)]
    # Spacing alone cannot tell a wrapped line from a new row here...
    assert len(table_of(words)) == 5
    # ...but the lines drawn between the rows can.
    rules = [(495, 100, 700, None), (585, 100, 700, None)]
    assert table_of(words, rules) == [
        ["RATING", "MEANING"],
        ["Severe and widespread", "Sustained outage of an important service"],
        ["Minor and contained", "Localised disruption absorbed in a day"],
    ]


def test_a_strip_used_by_one_row_is_not_a_column():
    rows = [
        ({110: "CASE", 400: "DATE", 700: "RAISED"}, 460),  # a header with its words spread out
        ({110: "CASE-0001", 400: "2026-01-01"}, 512),
        ({110: "CASE-0002", 400: "2026-01-02"}, 564),
        ({110: "CASE-0003", 400: "2026-01-03"}, 616),
    ]
    words = [w for key, (text_by_x, y) in enumerate(rows) for w in line(text_by_x, y, key)]
    assert table_of(words)[0] == ["CASE", "DATE RAISED"]
    assert table_of(words)[3] == ["CASE-0003", "2026-01-03"]


def test_two_columns_bridged_by_one_long_value_are_split():
    rows = [
        ({110: "NAME", 400: "ID", 560: "TEAM"}, 460),
        ({110: "Jane Sample", 400: "VND-00000", 560: "Sales"}, 512),
        ({110: "Omar Placeholder", 400: "VND-00001", 560: "Finance"}, 564),
        ({110: "Ana Example", 400: "VND-00002-LONGE", 560: "Legal"}, 616),  # ends where TEAM starts
        ({110: "Lee Specimen", 400: "VND-00003", 560: "Audit"}, 668),
    ]
    words = [w for key, (text_by_x, y) in enumerate(rows) for w in line(text_by_x, y, key)]
    assert table_of(words) == [
        ["NAME", "ID", "TEAM"],
        ["Jane Sample", "VND-00000", "Sales"],
        ["Omar Placeholder", "VND-00001", "Finance"],
        ["Ana Example", "VND-00002-LONGE", "Legal"],
        ["Lee Specimen", "VND-00003", "Audit"],
    ]


def test_words_that_line_up_inside_a_cell_do_not_split_it():
    rows = [
        ({110: "TIER", 400: "REVIEW"}, 460),
        ({110: "Tier 1 Critical", 400: "Quarterly"}, 512),
        ({110: "Tier 2 Material", 400: "Annual"}, 564),
        ({110: "Tier 3 Standard", 400: "Biennial"}, 616),
    ]
    words = [w for key, (text_by_x, y) in enumerate(rows) for w in line(text_by_x, y, key)]
    assert table_of(words)[1] == ["Tier 1 Critical", "Quarterly"]
