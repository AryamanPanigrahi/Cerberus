"""Structure-retention metric: how much of a document's structure survives extraction.

The brief asks that extracted text "retains no less than 80% of its original
structure" without saying how to measure it. Our definition:

    Of the structural elements in the source (headings, paragraphs, list
    items, table cells, text boxes), the share that appear in the extraction
    (a) in the right order relative to their neighbours, (b) as the right kind
    of element, and (c) under the right parent.

"The source" has to be written down by a person, because for a scan there is
no other record of what the page really says. The reference is a plain text
file in a small Markdown dialect, one element per line:

    # Heading            ## Sub-heading
    A paragraph.
    - A list item
    | Header A | Header B |      <- first row of a table is its header
    | cell     | cell     |
    [group]                      <- shapes that belong together (a card)
    > text inside the group
    [image]                      <- a picture or figure
    > a line of text read inside it

The workflow: export the extraction of a page in this format, correct it by
eye against the original, save it as the reference, then score. Starting from
the extraction saves typing; the person is still the judge of what is right.
An exported file starts with the word UNCHECKED. The person deletes that word
when they have finished; until then the file is not scored, because an
uncorrected export would score 100% against itself.

Units: a page (PDF), a slide (PPTX), or a top-level section (DOCX, which has
no pages; unit 0 is everything before the first heading).

Run:  python -m cerberus.eval.structure export FILE --units 3,25 --out labels/structure
      python -m cerberus.eval.structure score  FILE --refs labels/structure
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from ..extract.ir import Block, BlockType, Container, Document

SIMILAR = 0.8  # two texts this alike count as the same element (tolerates OCR slips)
_SKIPPED = {BlockType.METADATA, BlockType.HEADER, BlockType.FOOTER, BlockType.NOTES}
_CONTAINERS = {BlockType.GROUP: "group", BlockType.IMAGE_REGION: "image"}
_KINDS = {
    BlockType.PARAGRAPH: "paragraph",
    BlockType.TEXT_BOX: "paragraph",
    BlockType.LIST_ITEM: "list_item",
}


@dataclass
class Element:
    kind: str  # "h1", "h2", ..., "paragraph", "list_item", "cell"
    text: str
    image: bool = False  # text read inside a picture: scored separately
    stand_in: bool = False  # the lower part of a merged cell: gives its row a first cell, is not counted itself
    parent: tuple[str, ...] = ()  # filled in by _link
    # where it sits, used only to work out the parent:
    box: int | None = None  # which [group] / [image] it is inside
    table: int | None = None
    row: int = 0
    col: int = 0


# --- from the extraction ---------------------------------------------------


def units(doc: Document) -> dict[int, list[Block]]:
    """Split a document into the units a person checks one at a time."""
    result: dict[int, list[Block]] = {}
    section = 0
    for block in doc.blocks:
        if doc.format == "docx":
            if block.type is BlockType.HEADING and not block.section_path:
                section += 1
            if block.type is BlockType.METADATA and block.parent_id is None:
                continue
            result.setdefault(section, []).append(block)
        elif block.page is not None:
            result.setdefault(block.page, []).append(block)
    return result


def elements(doc: Document, blocks: list[Block]) -> list[Element]:
    """The scored elements of one unit, in reading order."""
    by_id = {b.block_id: b for b in doc.blocks}
    tables: dict[str, int] = {}
    boxes: dict[str, int] = {}
    out: list[Element] = []
    for block in blocks:
        chain = []  # the block's ancestors, nearest first
        parent = by_id.get(block.parent_id)
        while parent is not None:
            chain.append(parent)
            parent = by_id.get(parent.parent_id)
        if block.type in _SKIPPED or any(a.type in _SKIPPED for a in chain):
            continue  # page furniture and file properties are not document structure
        text = _one_line(block.text)
        box = next((a for a in chain if a.type in _CONTAINERS), None)
        box_index = boxes.setdefault(box.block_id, len(boxes)) if box is not None else None
        image = block.container is Container.IMAGE

        if block.type is BlockType.TABLE_CELL:
            table = tables.setdefault(block.parent_id, len(tables))
            origin = by_id.get((block.native_ref or {}).get("merged_from"))
            if origin is not None:
                text = _one_line(origin.text)
            out.append(Element("cell", text, image, stand_in=origin is not None, table=table, row=block.row, col=block.col))
        elif block.type is BlockType.HEADING:
            out.append(Element(f"h{len(block.section_path) + 1}", text, image, box=box_index))
        elif block.type in _KINDS:
            out.append(Element(_KINDS[block.type], text, image, box=box_index))
    return _link(out)


def to_markdown(doc: Document, blocks: list[Block]) -> str:
    """One unit of the extraction in the reference format, ready to be corrected."""
    by_id = {b.block_id: b for b in doc.blocks}
    lines: list[str] = []
    open_box = open_table = None

    def close():
        nonlocal open_box, open_table
        if open_box is not None or open_table is not None:
            lines.append("")
        open_box = open_table = None

    for block in blocks:
        chain = []
        parent = by_id.get(block.parent_id)
        while parent is not None:
            chain.append(parent)
            parent = by_id.get(parent.parent_id)
        if block.type in _SKIPPED or any(a.type in _SKIPPED for a in chain):
            continue
        text = _one_line(block.text)

        if block.type is BlockType.TABLE_CELL:
            origin = by_id.get((block.native_ref or {}).get("merged_from"))
            if origin is not None:  # the lower part of a merged cell reads as the cell it continues
                text = _one_line(origin.text)
            if open_table != (block.parent_id, block.row):
                if open_table is None or open_table[0] != block.parent_id:
                    close()
                    if lines and lines[-1]:
                        lines.append("")  # a blank line sets a table apart
                open_table = (block.parent_id, block.row)
                lines.append("|")
            lines[-1] += f" {text.replace('|', chr(92) + '|')} |"
            continue

        if block.type is BlockType.HEADING:
            line = "#" * (len(block.section_path) + 1) + " " + text
        elif block.type is BlockType.LIST_ITEM:
            line = "- " + text
        elif block.type in _KINDS:
            line = ("\\" if text[:1] in "#-|>[\\" else "") + text
        else:
            continue  # tables, groups, slides, image regions: shown through their content
        if not text:
            continue

        box = next((a for a in chain if a.type in _CONTAINERS), None)
        if box is None:
            close()
            lines.append(line)
        else:
            if open_box != box.block_id:
                close()
                if lines and lines[-1]:
                    lines.append("")
                lines.append(f"[{_CONTAINERS[box.type]}]")
                open_box = box.block_id
            lines.append("> " + line)
    return "\n".join(lines).strip() + "\n"


# --- from the reference ----------------------------------------------------


def parse_markdown(text: str) -> list[Element]:
    out: list[Element] = []
    box = table = None
    boxes = tables = 0
    row = 0
    image = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            box = table = None
            image = False
            continue
        if line in ("[group]", "[image]"):
            box, boxes, table = boxes, boxes + 1, None
            image = line == "[image]"
            continue
        inside = line.startswith(">")
        if inside:
            line = line[1:].strip()
            if box is None:
                box, boxes = boxes, boxes + 1
        else:
            box, image = None, False

        if line.startswith("|"):
            if table is None:
                table, tables, row = tables, tables + 1, 0
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            out += [Element("cell", c, image, table=table, row=row, col=i) for i, c in enumerate(cells)]
            row += 1
            continue
        table = None
        if match := re.match(r"(#+)\s+(.*)", line):
            out.append(Element(f"h{len(match.group(1))}", match.group(2), image, box=box))
        elif line.startswith("- "):
            out.append(Element("list_item", line[2:].strip(), image, box=box))
        else:
            out.append(Element("paragraph", line.removeprefix("\\"), image, box=box))
    return _link(out)


def _link(items: list[Element]) -> list[Element]:
    """Work out each element's parent the same way for both sides.

    - Inside a group or picture: the parent is that box, named by its first text.
    - A table cell: its column header and the first cell of its row.
    - Anything else: the nearest heading above it (for a heading, the nearest
      heading of a higher level).
    """
    first_in_box: dict[int, str] = {}
    cell_text: dict[tuple[int, int, int], str] = {}
    rows_in_table: dict[int, int] = {}
    for item in items:
        if item.box is not None and item.text:
            first_in_box.setdefault(item.box, item.text)
        if item.kind == "cell":
            cell_text[(item.table, item.row, item.col)] = item.text
            rows_in_table[item.table] = max(rows_in_table.get(item.table, 0), item.row + 1)

    headings: list[tuple[int, str]] = []
    for item in items:
        if item.kind == "cell":
            has_header = rows_in_table[item.table] > 1
            header = cell_text.get((item.table, 0, item.col), "") if has_header and item.row else ""
            first = min(c for (t, r, c) in cell_text if t == item.table and r == item.row)
            item.parent = ("cell", header, cell_text[(item.table, item.row, first)])
        elif item.box is not None:
            item.parent = ("box", first_in_box.get(item.box, ""))
        else:
            if item.kind.startswith("h"):
                level = int(item.kind[1:])
                while headings and headings[-1][0] >= level:
                    headings.pop()
            item.parent = ("under", headings[-1][1] if headings else "")
        if item.kind.startswith("h") and item.box is None:
            headings.append((int(item.kind[1:]), item.text))
    return [item for item in items if item.text and not item.stand_in]


# --- scoring ---------------------------------------------------------------


@dataclass
class Score:
    total: int = 0
    retained: int = 0
    missing: int = 0  # not found, or found out of order
    wrong_kind: int = 0
    wrong_parent: int = 0
    characters: float = 0.0  # summed similarity x length over matched elements
    length: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)  # kind -> [retained, total]

    @property
    def share(self) -> float:
        return self.retained / self.total if self.total else 1.0

    @property
    def character_accuracy(self) -> float:
        return self.characters / self.length if self.length else 1.0

    def add(self, other: Score) -> None:
        for name in ("total", "retained", "missing", "wrong_kind", "wrong_parent", "characters", "length"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for kind, (kept, total) in other.by_kind.items():
            mine = self.by_kind.setdefault(kind, [0, 0])
            mine[0] += kept
            mine[1] += total


def score(reference: list[Element], extracted: list[Element]) -> Score:
    """Compare one unit. Elements are paired by the longest run of matching
    texts that keeps both sequences in order, so an element that is present
    but in the wrong place counts as missing: its neighbours are wrong.
    """
    pairs = _align([e.text for e in reference], [e.text for e in extracted])
    result = Score(total=len(reference))
    for index, ref in enumerate(reference):
        kind = "heading" if ref.kind.startswith("h") else ref.kind
        tally = result.by_kind.setdefault(kind, [0, 0])
        tally[1] += 1
        if index not in pairs:
            result.missing += 1
            continue
        got = extracted[pairs[index]]
        result.characters += _similarity(_norm(ref.text), _norm(got.text)) * len(ref.text)
        result.length += len(ref.text)
        if got.kind != ref.kind:
            result.wrong_kind += 1
        elif not _same_parent(ref.parent, got.parent):
            result.wrong_parent += 1
        else:
            result.retained += 1
            tally[0] += 1
    return result


def _align(reference: list[str], extracted: list[str]) -> dict[int, int]:
    """Longest common subsequence under fuzzy equality: reference index -> extracted index."""
    a, b = [_norm(t) for t in reference], [_norm(t) for t in extracted]
    n, m = len(a), len(b)
    same = [[_similarity(x, y) >= SIMILAR for y in b] for x in a]
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = best[i + 1][j + 1] + 1 if same[i][j] else max(best[i + 1][j], best[i][j + 1])
    pairs, i, j = {}, 0, 0
    while i < n and j < m:
        if same[i][j]:
            pairs[i] = j
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _same_parent(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return (
        len(a) == len(b)
        and a[0] == b[0]
        and all(_similarity(_norm(x), _norm(y)) >= SIMILAR for x, y in zip(a[1:], b[1:]))
    )


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _one_line(text: str) -> str:
    return " ".join(text.split())


@lru_cache(maxsize=200_000)
def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b or min(len(a), len(b)) < SIMILAR * max(len(a), len(b)):
        return 0.0  # lengths too different to reach the threshold
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    return matcher.ratio() if matcher.quick_ratio() >= SIMILAR else 0.0


# --- command line ----------------------------------------------------------


def _reference_path(folder: Path, doc: Document, unit: int) -> Path:
    return folder / f"{Path(doc.name).stem}.{unit:03d}.md"


def main(argv: list[str] | None = None) -> None:
    from ..extract import extract

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write units of the extraction to correct by hand")
    export.add_argument("file")
    export.add_argument("--units", required=True, help="pages, slides or sections, e.g. 3,25")
    export.add_argument("--out", required=True)
    scoring = commands.add_parser("score", help="score the extraction against corrected references")
    scoring.add_argument("file")
    scoring.add_argument("--refs", required=True)
    args = parser.parse_args(argv)

    doc = extract(args.file)
    by_unit = units(doc)
    if args.command == "export":
        folder = Path(args.out)
        folder.mkdir(parents=True, exist_ok=True)
        for unit in (int(u) for u in args.units.split(",")):
            path = _reference_path(folder, doc, unit)
            if path.exists():
                print(f"kept    {path} (already there; delete it to export again)")
                continue
            note = (
                f"<!-- UNCHECKED {doc.name}, unit {unit}. Correct this to match the original, "
                "then delete the word UNCHECKED and save. -->\n"
            )
            path.write_text(note + to_markdown(doc, by_unit.get(unit, [])), encoding="utf-8")
            print(f"wrote   {path}")
        return

    total, image_total = Score(), Score()
    print(f"{'unit':>5} {'elements':>9} {'retained':>9} {'share':>7} {'missing':>8} {'kind':>5} {'parent':>7} {'chars':>7}")
    for unit in sorted(by_unit):
        path = _reference_path(Path(args.refs), doc, unit)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "UNCHECKED" in text.split("\n", 1)[0]:
            print(f"{unit:>5}  not scored: {path.name} is still marked UNCHECKED")
            continue
        reference = parse_markdown(text)
        extracted = elements(doc, by_unit[unit])
        one = score([e for e in reference if not e.image], [e for e in extracted if not e.image])
        image_total.add(score([e for e in reference if e.image], [e for e in extracted if e.image]))
        total.add(one)
        print(f"{unit:>5} {one.total:>9} {one.retained:>9} {one.share:>7.1%} {one.missing:>8} "
              f"{one.wrong_kind:>5} {one.wrong_parent:>7} {one.character_accuracy:>7.1%}")
    if not total.total:
        print("no reference files found for this document")
        return
    print(f"{'all':>5} {total.total:>9} {total.retained:>9} {total.share:>7.1%} {total.missing:>8} "
          f"{total.wrong_kind:>5} {total.wrong_parent:>7} {total.character_accuracy:>7.1%}")
    for kind, (kept, count) in sorted(total.by_kind.items()):
        print(f"      {kind:<10} {kept}/{count}")
    if image_total.total:
        print(f"text inside pictures (scored separately): {image_total.retained}/{image_total.total} "
              f"= {image_total.share:.1%}, character accuracy {image_total.character_accuracy:.1%}")


if __name__ == "__main__":
    main()
