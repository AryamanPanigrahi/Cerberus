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
