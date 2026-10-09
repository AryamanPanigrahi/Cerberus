"""Structure retention for DOCX and PPTX, over the whole file, with no hand-made reference.

A scan has no record of its own structure, so a person has to write one down
(structure.py). A DOCX or PPTX does have one: the file says which paragraph is
a heading, which cell is in which row and column, which shape is on which
slide. This module reads that record straight from the file, with a few lines
of python-docx / python-pptx that share no code with the extractors, and
checks the extraction against it. Every element of the file is counted, so
the figure is "N of M elements, whole file", not a sample.

What counts as retained (the definition in structure.py):

    the element is in the extraction, with its text, as the right kind of
    element, under the right parent, and in the right order.

- DOCX. The file fixes the order, so order is scored: elements are paired by
  the longest run of matching texts that keeps both sequences in order. The
  parent of a paragraph is the heading above it; of a cell, its column header
  and the first cell of its row.
- PPTX. A slide is shapes placed on a canvas; the file stores them in drawing
  order, which is not reading order, and nothing in it says what the reading
  order is. So order is not scored for shapes: an element is retained if it
  is on the right slide as the right kind. Table cells are scored as in DOCX.

Left out of the reference, and said so in the report: text boxes, headers,
footers and footnotes in a DOCX, and tables nested inside a cell. They are
extracted, but the few lines here do not enumerate them.

Run:  python -m cerberus.eval.native FILE [FILE ...]
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from . import structure
from .structure import Element, Score

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# --- DOCX ---------------------------------------------------------------------


def docx_reference(path: str | Path) -> list[Element]:
    """Every body paragraph and table cell of a DOCX, in file order, with its kind and parent."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    source = docx.Document(str(path))
    items: list[Element] = []
    levels: list[int] = []  # heading levels currently open, outermost first
    tables = 0

    def walk(parent) -> None:
        nonlocal tables
        for child in parent.iterchildren():
            if child.tag == f"{_W}sdt":  # a content control: its contents are ordinary body content
                content = child.find(f"{_W}sdtContent")
                if content is not None:
                    walk(content)
            elif child.tag == f"{_W}p":
                paragraph = Paragraph(child, source)
                text = structure._one_line(paragraph.text)
                style = (paragraph.style.name if paragraph.style is not None else "") or ""
                heading = re.fullmatch(r"Heading (\d)", style)
                if heading and text:
                    level = int(heading.group(1))
                    while levels and levels[-1] >= level:
                        levels.pop()
                    levels.append(level)
                    items.append(Element(f"h{len(levels)}", text))
                elif _numbered(child, paragraph):
                    items.append(Element("list_item", text))
                else:
                    items.append(Element("paragraph", text))
            elif child.tag == f"{_W}tbl":
                table = Table(child, source)
                for r, row in enumerate(table._tbl.tr_lst):
                    column = 0
                    for cell in row.tc_lst:
                        text = structure._one_line(" ".join(Paragraph(p, source).text for p in cell.p_lst))
                        items.append(Element("cell", text, table=tables, row=r, col=column))
                        column += cell.grid_span
                tables += 1

    walk(source.element.body)
    return structure._link(items)


def _numbered(element, paragraph) -> bool:
    """Is the paragraph a list item: numbered or bulleted directly, or through its style?"""
    if element.find(f"{_W}pPr/{_W}numPr") is not None:
        return True
    style = paragraph.style
    while style is not None:
        properties = style.element.find(f"{_W}pPr")
        if properties is not None and properties.find(f"{_W}numPr") is not None:
            return True
        style = style.base_style
    return False


# --- PPTX ---------------------------------------------------------------------


def pptx_reference(path: str | Path) -> dict[int, list[Element]]:
    """For each slide: its text-bearing shapes and table cells, with kind and parent."""
    from pptx import Presentation
    from pptx.shapes.group import GroupShape

    slides: dict[int, list[Element]] = {}
    for number, slide in enumerate(Presentation(str(path)).slides, start=1):
        title = slide.shapes.title
        items: list[Element] = []
        tables = 0

        def walk(shapes) -> None:
            nonlocal tables
            for shape in shapes:
                if isinstance(shape, GroupShape):
                    walk(shape.shapes)
                elif getattr(shape, "has_table", False) and shape.has_table:
                    for r, row in enumerate(shape.table.rows):
                        for c, cell in enumerate(row.cells):
                            items.append(Element("cell", structure._one_line(cell.text_frame.text), table=tables, row=r, col=c))
                    tables += 1
                elif shape.has_text_frame and shape.text_frame.text.strip():
                    is_title = title is not None and shape.shape_id == title.shape_id
                    items.append(Element("h1" if is_title else "paragraph", structure._one_line(shape.text_frame.text)))

        walk(slide.shapes)
        linked = structure._link(items)  # gives cells their header and row
        for item in linked:
            if item.kind != "cell":
                item.parent = ("slide", str(number))
        slides[number] = linked
    return slides


# --- scoring ------------------------------------------------------------------


def score_docx(path: str | Path, doc) -> Score:
    """Whole-file score of a DOCX extraction against the file's own structure. Order is scored."""
    reference = docx_reference(path)
    extracted = [e for e in structure.elements(doc, doc.blocks) if not e.image]
    return structure.score(reference, extracted)


def score_pptx(path: str | Path, doc) -> Score:
    """Whole-file score of a PPTX extraction. Shapes: right slide and kind. Cells: right header and row."""
    total = Score()
    by_slide = structure.units(doc)
    for number, reference in pptx_reference(path).items():
        extracted = [e for e in structure.elements(doc, by_slide.get(number, [])) if not e.image]
        for item in extracted:
            if item.kind != "cell":
                item.parent = ("slide", str(number))
        total.add(_unordered(reference, extracted))
    return total


def _unordered(reference: list[Element], extracted: list[Element]) -> Score:
    """Pair elements by text alone, best match first, each extracted element used once."""
    result = Score(total=len(reference))
    free = list(range(len(extracted)))
    for ref in reference:
        kind = "heading" if ref.kind.startswith("h") else ref.kind
        tally = result.by_kind.setdefault(kind, [0, 0])
        tally[1] += 1
        scored = [(structure._similarity(structure._norm(ref.text), structure._norm(extracted[i].text)), -i) for i in free]
        # Among equally good texts (the same name in three cells) prefer one that is also in the right place.
        scored = [(s, extracted[-i].kind == ref.kind and structure._same_parent(ref.parent, extracted[-i].parent), i)
                  for s, i in scored if s >= structure.SIMILAR]
        if not scored:
            result.missing += 1
            continue
        similarity, _, negative = max(scored)
        got = extracted[-negative]
        free.remove(-negative)
        result.characters += similarity * len(ref.text)
        result.length += len(ref.text)
        if got.kind != ref.kind:
            result.wrong_kind += 1
        elif not structure._same_parent(ref.parent, got.parent):
            result.wrong_parent += 1
        else:
            result.retained += 1
            tally[0] += 1
    return result


def report(name: str, result: Score, ordered: bool) -> str:
    lines = [
        f"{name}: {result.retained} of {result.total} elements retained = {result.share:.1%} "
        f"(whole file; order {'scored' if ordered else 'not scored: a slide has no reading order of its own'})",
        f"  lost: {result.missing} missing or out of order, {result.wrong_kind} wrong kind, {result.wrong_parent} wrong parent",
        f"  character accuracy of matched elements: {result.character_accuracy:.1%}",
    ]
    lines += [f"  {kind:<10} {kept} of {count}" for kind, (kept, count) in sorted(result.by_kind.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    from ..extract import extract

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("files", nargs="+", help="DOCX and PPTX files")
    args = parser.parse_args(argv)
    for path in map(Path, args.files):
        doc = extract(path)
        if doc.format == "docx":
            print(report(path.name, score_docx(path, doc), ordered=True))
        elif doc.format == "pptx":
            print(report(path.name, score_pptx(path, doc), ordered=False))
        else:
            print(f"{path.name}: a {doc.format} has no structure of its own to read; use cerberus.eval.structure")


if __name__ == "__main__":
    main()
