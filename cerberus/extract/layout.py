"""Layout analysis: OCR words -> headings, paragraphs, list items and tables.

OCR returns words with positions and nothing else. Structure is rebuilt from
geometry alone, so this module needs no PDF or image library and works on any
OCR'd picture (a scanned page, a screenshot inside a DOCX).

Tables are the hard part. Many have no vertical rules, so columns cannot be
found by looking for lines. Instead:
- a line is "tabular" if it has a gap much wider than a normal word space;
- consecutive tabular lines, plus the wrapped lines between them, form a table;
- columns are the vertical strips of white space that no line of the table crosses;
- rows are split where the line spacing jumps (a new row has padding above it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from .ocr import OcrWord

# "4. Scope", "4.2 Scope", and appendix style "A.2 Scale".
_NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2})*|[A-Z](?:\.\d{1,2})+)\.?\s+\S")
_LIST_MARKER = re.compile(r"^(?:\d{1,2}[.)]|[A-Za-z][.)]|\([A-Za-z0-9]{1,3}\)|[•·▪◦‣*–—-])\s+\S")
_DESCENDERS = set("gjpqy,;()[]{}/|_Q@$")


@dataclass
class Line:
    words: list[OcrWord]

    def __post_init__(self):
        self.words.sort(key=lambda w: w.x0)
        self.x0 = min(w.x0 for w in self.words)
        self.x1 = max(w.x1 for w in self.words)
        self.y0 = min(w.y0 for w in self.words)
        self.y1 = max(w.y1 for w in self.words)
        # Where the line "is" for spacing purposes. The median ignores the odd
        # word whose box is stretched by a bracket or a ruled line.
        self.top = median(w.y0 for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def segments(self, gap: float) -> list[list[OcrWord]]:
        """Runs of words separated by more than `gap`: the cells of a table row."""
        runs = [[self.words[0]]]
        for previous, word in zip(self.words, self.words[1:]):
            if word.x0 - previous.x1 > gap:
                runs.append([])
            runs[-1].append(word)
        return runs

    def cap_height(self) -> float | None:
        """Height of the capital letters, ignoring words whose box a descender stretches."""
        heights = [
            w.height
            for w in self.words
            if (w.text[0].isupper() or w.text[0].isdigit()) and not set(w.text) & _DESCENDERS
        ]
        return median(heights) if heights else None


@dataclass
class TextRegion:
    kind: str  # "heading", "list_item" or "paragraph"
    lines: list[Line]
    level: int | None = None  # headings only

    @property
    def y0(self) -> float:
        return self.lines[0].y0


@dataclass
class TableRegion:
    columns: list[tuple[float, float]]  # x range of each column
    rows: list[tuple[float, float]] = field(default_factory=list)  # y range of each row
    cells: list[list[list[OcrWord]]] = field(default_factory=list)  # [row][column] -> words

    @property
    def y0(self) -> float:
        return self.rows[0][0]


@dataclass
class _Scale:
    """Typical sizes on this page; every threshold is a multiple of one of these."""

    height: float  # word box height
    space: float  # gap between adjacent words
    cap: float  # capital-letter height of body text
    pitch: float  # distance between successive lines of a paragraph

    @property
    def cell_gap(self) -> float:
        return max(3 * self.space, 0.9 * self.height)


def build_lines(words: list[OcrWord]) -> list[Line]:
    """Group words into lines, top to bottom.

    The engine's own line grouping is the starting point; lines it split that
    sit side by side on the same baseline are joined back together.
    """
    grouped: dict[tuple, list[OcrWord]] = {}
    for word in words:
        grouped.setdefault(word.line, []).append(word)
    lines = sorted((Line(ws) for ws in grouped.values()), key=lambda ln: (ln.y0, ln.x0))

    merged: list[Line] = []
    for line in lines:
        for other in reversed(merged[-4:]):
            overlap = min(line.y1, other.y1) - max(line.y0, other.y0)
            side_by_side = line.x0 >= other.x1 or line.x1 <= other.x0
            if side_by_side and overlap >= 0.6 * min(line.y1 - line.y0, other.y1 - other.y0):
                other.words.extend(line.words)
                other.__post_init__()
                break
        else:
            merged.append(line)
    return sorted(merged, key=lambda ln: (ln.y0, ln.x0))


def analyse(lines: list[Line]) -> list[TextRegion | TableRegion]:
    """Lines (top to bottom) -> regions in reading order. Assumes one text column."""
    if not lines:
        return []
    scale = _measure(lines)
    regions: list[TextRegion | TableRegion] = []
    pending: list[Line] = []  # text lines waiting to be split into paragraphs

    def flush():
        regions.extend(_paragraphs(pending, scale))
        pending.clear()

    i = 0
    while i < len(lines):
        end = _table_end(lines, i, scale)
        if end is None:
            pending.append(lines[i])
            i += 1
            continue
        start = i
        # A header that wraps puts a short line just above the first full row.
        if pending and _continues(pending[-1], lines[i], lines[start:end], scale):
            start -= 1
            pending.pop()
        flush()
        regions.append(_table(lines[start:end], scale))
        i = end
    flush()
    return regions


def _measure(lines: list[Line]) -> _Scale:
    """Typical sizes of the page's body text.

    The body size is the one that carries the most characters, not the median:
    a figure full of small print can outnumber the body lines and would drag a
    median down, making ordinary text look like headings.
    """
    sized = [(line.cap_height(), line) for line in lines]
    sized = [(cap, line) for cap, line in sized if cap]
    if not sized:
        height = median(w.height for line in lines for w in line.words)
        return _Scale(height, 0.3 * height, height, 1.9 * height)
    cap = max(
        (c for c, _ in sized),
        key=lambda c: sum(len(line.text) for other, line in sized if c <= other <= 1.2 * c),
    )
    body = [line for other, line in sized if 0.8 * cap <= other <= 1.3 * cap]
    is_body = {id(line) for line in body}
    height = median(w.height for line in body for w in line.words)
    gaps = [
        b.x0 - a.x1
        for line in body
        for a, b in zip(line.words, line.words[1:])
        if 0 < b.x0 - a.x1 < 2 * height
    ]
    pitches = [
        b.top - a.top
        for a, b in zip(lines, lines[1:])
        if id(a) in is_body and id(b) in is_body and 0.8 * height < b.top - a.top < 3 * height
    ]
    return _Scale(
        height=height,
        space=median(gaps) if gaps else 0.3 * height,
        cap=median(c for c, line in sized if id(line) in is_body),
        pitch=median(pitches) if len(pitches) >= 3 else 1.9 * height,
    )


def _heading_level(line: Line, scale: _Scale) -> int | None:
    """Heading test for a single line: short, no sentence ending, and either
    multi-part numbering ("4.2 Scope") or clearly larger type than the body.

    Word-box heights from OCR are noisy, so size alone only decides when the
    difference is large; "3. Title" and a list item "3. Do this." look alike
    otherwise.
    """
    text = line.text
    if len(line.words) > 14 or text.rstrip()[-1:] in ".;:,":
        return None
    if len(line.segments(scale.cell_gap)) > 1:  # spread across columns: a table row
        return None
    cap = line.cap_height()
    large = cap is not None and cap >= 1.4 * scale.cap
    numbered = _NUMBERED_HEADING.match(text)
    if numbered:
        parts = numbered.group(1).count(".") + 1
        return parts if parts > 1 or large else None
    return 1 if large and len(line.words) <= 10 else None


# --- tables ---------------------------------------------------------------


def _table_end(lines: list[Line], start: int, scale: _Scale) -> int | None:
    """If a table starts at lines[start], the index one past its last line."""
    if len(lines[start].segments(scale.cell_gap)) < 2:
        return None
    end, tabular = start + 1, 1
    while end < len(lines):
        line, previous = lines[end], lines[end - 1]
        # Sizes are taken from the table itself: its type is often smaller than the body's.
        height = median(w.height for ln in lines[start:end] for w in ln.words)
        if line.top - previous.top > 4.5 * height:  # too far below to belong
            break
        if _heading_level(line, scale) is not None:
            break
        if len(line.segments(scale.cell_gap)) >= 2:
            tabular += 1
        elif not _fits(line, _columns(lines[start:end], scale), scale):
            break
        end += 1
    if tabular < 2 or len(_columns(lines[start:end], scale)) < 2:
        return None
    return end


def _continues(above: Line, first: Line, table: list[Line], scale: _Scale) -> bool:
    return (
        first.top - above.top <= 2.2 * scale.height
        and _heading_level(above, scale) is None
        and _fits(above, _columns(table, scale), scale)
    )


def _columns(lines: list[Line], scale: _Scale) -> list[tuple[float, float]]:
    """Vertical strips of ink separated by white space that no line crosses."""
    spans = sorted((w.x0, w.x1) for line in lines for w in line.words)
    columns = [list(spans[0])]
    for x0, x1 in spans[1:]:
        if x0 - columns[-1][1] > 0.5 * scale.height:
            columns.append([x0, x1])
        else:
            columns[-1][1] = max(columns[-1][1], x1)
    return [(a, b) for a, b in columns]


def _column_of(word: OcrWord, columns: list[tuple[float, float]]) -> int:
    centre = (word.x0 + word.x1) / 2
    for index, (_, x1) in enumerate(columns):
        if centre <= x1:
            return index
    return len(columns) - 1


def _fits(line: Line, columns: list[tuple[float, float]], scale: _Scale) -> bool:
    """A wrapped line of a cell: every unbroken run of its words stays inside one column."""
    if len(columns) < 2:
        return False
    for run in line.segments(scale.space * 2.5):
        first = _column_of(run[0], columns)
        if _column_of(run[-1], columns) != first:
            return False
        # ...and does not reach into the next column's strip.
        if first + 1 < len(columns) and run[-1].x1 > columns[first + 1][0] - 0.25 * scale.height:
            return False
    return True


def _table(lines: list[Line], scale: _Scale) -> TableRegion:
    columns = _columns(lines, scale)
    table = TableRegion(columns)

    # Wrapped lines of a cell sit closer together than rows do, because a new
    # row has padding above it. Look for that step in the line spacings.
    pitches = [b.top - a.top for a, b in zip(lines, lines[1:])]
    filled = [len({_column_of(w, columns) for w in line.words}) for line in lines]
    ordered = sorted(pitches)
    threshold = 0.0  # no step: every line is its own row
    for a, b in zip(ordered, ordered[1:]):
        if a > 0 and b / a >= 1.2:
            threshold = (a + b) / 2
            break
    if threshold:
        # A real wrapped line fills fewer columns than the line that starts a
        # row. If the "wrapped" lines are as full as the rest, the step was
        # something else (a tall header band) and is ignored.
        wrapped = [n for n, pitch in zip(filled[1:], pitches) if pitch <= threshold]
        starts = [filled[0]] + [n for n, pitch in zip(filled[1:], pitches) if pitch > threshold]
        if sum(wrapped) / len(wrapped) > 0.85 * sum(starts) / len(starts):
            threshold = 0.0

    groups = [[lines[0]]]
    for line, pitch in zip(lines[1:], pitches):
        if pitch > threshold:
            groups.append([])
        groups[-1].append(line)

    for group in groups:
        cells: list[list[OcrWord]] = [[] for _ in columns]
        for line in group:  # top to bottom, each already left to right
            for word in line.words:
                cells[_column_of(word, columns)].append(word)
        table.rows.append((min(ln.y0 for ln in group), max(ln.y1 for ln in group)))
        table.cells.append(cells)
    return table


# --- running text -----------------------------------------------------------


def _paragraphs(lines: list[Line], scale: _Scale) -> list[TextRegion]:
    regions: list[TextRegion] = []
    for index, line in enumerate(lines):
        level = _heading_level(line, scale)
        is_item = level is None and _LIST_MARKER.match(line.text) is not None
        gap = line.top - lines[index - 1].top if index else None
        starts_new = (
            not regions
            or level is not None
            or is_item
            or regions[-1].kind == "heading"
            or gap > 1.25 * scale.pitch
        )
        if not starts_new:
            regions[-1].lines.append(line)
        elif level is not None:
            regions.append(TextRegion("heading", [line], level))
        else:
            regions.append(TextRegion("list_item" if is_item else "paragraph", [line]))
    return regions
