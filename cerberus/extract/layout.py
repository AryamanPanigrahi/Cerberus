"""Layout analysis: OCR words -> headings, paragraphs, list items and tables.

OCR returns words with positions and nothing else. Structure is rebuilt from
geometry alone, so this module needs no PDF or image library and works on any
OCR'd picture (a scanned page, a screenshot inside a DOCX).

Tables are the hard part. Many have no vertical rules, so columns cannot be
found by looking for lines. Instead:
- a line is "tabular" if it has a gap much wider than a normal word space;
- consecutive tabular lines, plus the wrapped lines between them, form a table;
- columns are the vertical strips of white space that no line of the table crosses;
- rows are the bands between ruled lines if the table has them, otherwise they
  are split where the line spacing jumps (a new row has padding above it);
- a strip only one row uses is merged away, and a strip hiding two columns is
  split where most rows start a word at the same x.
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


def analyse(lines: list[Line], rules=()) -> list[TextRegion | TableRegion]:
    """Lines (top to bottom) -> regions in reading order. Assumes one text column.

    rules: horizontal lines drawn on the page, as (y, x0, x1, band_top), if the
    caller has the page image (see figures.horizontal_rules). They are optional
    evidence: a dark header band followed by ruled lines marks out a table
    exactly, which is more reliable than judging by the gaps between words.
    """
    if not lines:
        return []
    scale = _measure(lines)
    regions: list[TextRegion | TableRegion] = []
    pending: list[Line] = []  # text lines waiting to be split into paragraphs

    def flush():
        regions.extend(_paragraphs(pending, scale))
        pending.clear()

    ruled = dict(_ruled_spans(lines, rules, scale))
    i = 0
    while i < len(lines):
        if i in ruled:
            flush()
            regions.append(_table(lines[i : ruled[i]], scale, rules))
            i = ruled[i]
            continue
        stop = min((start for start in ruled if start > i), default=len(lines))
        end = _table_end(lines, i, scale, stop)
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
        regions.append(_table(lines[start:end], scale, rules))
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


def _ruled_spans(lines: list[Line], rules, scale: _Scale) -> list[tuple[int, int]]:
    """Tables drawn with a dark header band and ruled lines, as line index ranges.

    The table runs from the top of the band down through each following rule
    of the same width, as long as there is text between one rule and the next.
    It stops at the next band, at a heading, or at a long empty gap.
    """
    def centre(line):
        return (line.y0 + line.y1) / 2

    spans: list[tuple[int, int]] = []
    ordered = sorted(rules)
    slack = 2 * scale.height
    for index, band in enumerate(ordered):
        if band[3] is None:
            continue
        last, bottom = band[0], None
        for rule in ordered[index + 1 :]:
            if rule[3] is not None:  # the next table's header
                break
            if abs(rule[1] - band[1]) > slack or abs(rule[2] - band[2]) > slack:
                continue
            if rule[0] - last < 0.5 * scale.height:  # a doubled line
                last = rule[0]
                continue
            between = [ln for ln in lines if last < centre(ln) < rule[0]]
            if not between or rule[0] - last > 14 * scale.height:
                break
            if any(_heading_level(ln, scale) is not None for ln in between):
                break
            last = bottom = rule[0]
        if bottom is None:
            continue
        inside = [k for k, ln in enumerate(lines) if band[3] <= centre(ln) <= bottom]
        if inside and (not spans or inside[0] >= spans[-1][1]):
            spans.append((inside[0], inside[-1] + 1))
    return spans


def _table_end(lines: list[Line], start: int, scale: _Scale, stop: int) -> int | None:
    """If a table starts at lines[start], the index one past its last line (at most stop)."""
    if len(lines[start].segments(scale.cell_gap)) < 2:
        return None
    end, tabular = start + 1, 1
    while end < stop:
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


def _table(lines: list[Line], scale: _Scale, rules=()) -> TableRegion:
    columns = _columns(lines, scale)
    groups = _rows_from_rules(lines, rules, scale) or _rows_from_spacing(lines, columns)
    columns = _split_columns(_merge_columns(columns, groups), groups, scale)

    table = TableRegion(columns)
    for group in groups:
        cells: list[list[OcrWord]] = [[] for _ in columns]
        for line in group:  # top to bottom, each already left to right
            for word in line.words:
                cells[_column_of(word, columns)].append(word)
        table.rows.append((min(ln.y0 for ln in group), max(ln.y1 for ln in group)))
        table.cells.append(cells)
    return table


def _rows_from_rules(lines: list[Line], rules, scale: _Scale) -> list[list[Line]] | None:
    """Rows as the bands between horizontal lines drawn across the table.

    This is the reliable signal when it exists: a cell that wraps onto three
    lines is still one band. It is used only if the lines cut the table into at
    least three bands; a single rule under the header says nothing about the
    rows below it.
    """
    left, right = min(ln.x0 for ln in lines), max(ln.x1 for ln in lines)
    top, bottom = lines[0].y0, lines[-1].y1
    slack = 2 * scale.height
    cuts: list[float] = []
    for y, x0, x1, _ in sorted(rules):
        spans = x0 <= left + slack and x1 >= right - slack
        if spans and top < y < bottom and (not cuts or y - cuts[-1] > 0.5 * scale.height):
            cuts.append(y)
    bands: dict[int, list[Line]] = {}
    for line in lines:
        centre = (line.y0 + line.y1) / 2
        bands.setdefault(sum(cut < centre for cut in cuts), []).append(line)
    return [bands[key] for key in sorted(bands)] if len(bands) >= 3 else None


def _rows_from_spacing(lines: list[Line], columns) -> list[list[Line]]:
    """Rows from line spacing, for tables with no ruled lines between rows.

    Wrapped lines of a cell sit closer together than rows do, because a new
    row has padding above it. Look for that step in the line spacings.
    """
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
    return groups


def _merge_columns(columns, groups: list[list[Line]]) -> list[tuple[float, float]]:
    """Drop columns that only one row uses.

    White-space strips over-split when a header's words are spread out, or a
    wrapped word lands on its own: the stray strip has text in a single row. A
    real column has text in at least two rows.
    """
    if len(groups) < 3:
        return columns
    support = [0] * len(columns)
    for group in groups:
        for index in {_column_of(w, columns) for line in group for w in line.words}:
            support[index] += 1
    merged: list[list[float]] = []
    for (x0, x1), rows in zip(columns, support):
        if merged and rows < 2:
            merged[-1][1] = x1
        else:
            merged.append([x0, x1])
    if len(merged) > 1 and support[0] < 2:  # a stray first strip joins its right neighbour
        merged[1][0] = merged[0][0]
        merged.pop(0)
    return [(a, b) for a, b in merged]


def _split_columns(columns, groups: list[list[Line]], scale: _Scale) -> list[tuple[float, float]]:
    """Split a strip that is really two columns whose text nearly touches in some row.

    One long value closing the gap in a single row is enough to hide the white
    space between two columns. The second column still shows: in most rows a
    word starts at exactly the same x, after a gap wider than a word space.
    Words inside a cell also line up sometimes ("Tier 1", "Tier 2"), but only a
    single space apart, which is why the gap is checked.
    """
    tolerance = 0.4 * scale.height
    result: list[tuple[float, float]] = []
    for index, (x0, x1) in enumerate(columns):
        starts: list[tuple[float, float, int]] = []  # (word x, gap before it, row)
        used = set()
        for row, group in enumerate(groups):
            for line in group:
                inside = [w for w in line.words if _column_of(w, columns) == index]
                used.update([row] if inside else [])
                starts += [(b.x0, b.x0 - a.x1, row) for a, b in zip(inside, inside[1:])]
        cut = None
        for x, _, _ in sorted(starts):
            near = [s for s in starts if abs(s[0] - x) <= tolerance]
            rows = {row for _, _, row in near}
            wide = sorted(gap for _, gap, _ in near)[len(near) // 2] >= 2 * scale.space
            crossing = sum(
                1
                for group in groups
                for line in group
                for w in line.words
                if _column_of(w, columns) == index and w.x0 < x - tolerance < w.x1
            )
            if len(rows) >= max(3, 0.6 * len(used)) and wide and crossing == 0:
                cut = min(s[0] for s in near)
                break
        if cut is None:
            result.append((x0, x1))
        else:
            result += [(x0, cut - tolerance), (cut - tolerance / 2, x1)]
    return result


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


def compose(words: list[OcrWord]) -> tuple[str, list[tuple[int, int, OcrWord]]]:
    """Join OCR words into block text, returning each word's character span in it.

    Words are separated by one space. A word broken by a hyphen at a line end
    is rejoined; the wrap shows as the next word starting further left.
    """
    text, spans = "", []
    for previous, word in zip([None, *words], words):
        if previous is not None:
            wrapped = word.x0 < previous.x0
            if not (wrapped and previous.text.endswith("-") and len(previous.text) > 1):
                text += " "
        spans.append((len(text), len(text) + len(word.text), word))
        text += word.text
    return text, spans
