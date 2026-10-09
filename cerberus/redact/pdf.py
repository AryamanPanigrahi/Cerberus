"""Masked copy of a PDF: same pages, same layout, tokens where the PII was.

Covering is not removing. A box drawn over a name leaves the name in the file
underneath it. Here each rectangle is marked as a redaction and then applied,
which erases what lies under it: text, and the pixels of a scanned page image.
The token is then written in the cleared space.

What is masked:

- every finding, at the boxes of the words it covers;
- inside a figure that two OCR engines read and agreed on: every finding, every
  word the engines disagreed on, and any face or barcode. No token is written
  inside a picture: a label there would be read back as text by the next OCR;
- every other figure, as a whole region;
- every quarantined page, entirely.

The file's own properties are removed too. A PDF keeps them twice (the info
dictionary and an XMP packet), so both are cleared, and the file is rebuilt
from scratch on save (garbage collection, no incremental update) so that
nothing removed is still sitting in it.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from ..extract.ir import BlockType, Document, Unit
from . import office
from .tokens import Tokenized

_FILL = (0.88, 0.88, 0.88)  # light grey, so a masked area is visible as such
_PAD = 1.0  # points added round each box, to cover the soft edges of scanned letters


def mask_pdf(
    source: str | Path, doc: Document, tokenized: list[Tokenized], out: str | Path, pictures: str = "findings"
) -> Path:
    """Write a masked copy of `source` to `out`. `doc` is the extraction of the same file."""
    blocks = {b.block_id: b for b in doc.blocks}
    figures = [b for b in doc.blocks if b.type is BlockType.IMAGE_REGION and b.bbox is not None]
    quarantined = {p.number for p in doc.pages if p.quarantined}
    inside = office.picture_boxes(doc, tokenized, pictures)  # figure -> rectangles, or None for the whole figure

    marks: dict[int, list[tuple[pymupdf.Rect, str]]] = {}
    with pymupdf.open(str(source)) as pdf:
        for number in quarantined:
            marks.setdefault(number, []).append((pdf[number - 1].rect, "[page masked]"))
        for figure in figures:
            if figure.page in quarantined:
                continue
            boxes = inside.get(figure.block_id)
            if boxes is None:
                marks.setdefault(figure.page, []).append((_rect(figure.bbox), "[figure masked]"))
            else:
                marks.setdefault(figure.page, []).extend((pymupdf.Rect(*box), "") for box in boxes)

        for item in tokenized:
            f = item.finding
            block = blocks.get(f.block_id)
            if f.doc_id != doc.doc_id or block is None or block.page is None or block.page in quarantined:
                continue
            if f.location == "image":
                continue  # inside a figure: handled with the figure, above
            boxes = [_rect(b) for b in block.span_boxes(f.start, f.end) if b.unit is Unit.PT]
            if not boxes and block.bbox is not None:
                # A typed page has no word boxes: ask the PDF where the text is.
                boxes = pdf[block.page - 1].search_for(f.text, clip=_rect(block.bbox) + (-2, -2, 2, 2))
            for index, rect in enumerate(_by_line(boxes)):
                # A value wrapped over two lines gets its token on the first.
                marks.setdefault(block.page, []).append((rect, item.token if index == 0 else ""))

        for number, items in marks.items():
            page = pdf[number - 1]
            padded = [(rect + (-_PAD, -_PAD, _PAD, _PAD), label) for rect, label in items]
            for rect, _ in padded:
                page.add_redact_annot(rect, fill=_FILL, cross_out=False)
            # Erase what is under each rectangle, including scanned pixels.
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
            # Then write each token in the cleared space, sized to fit it.
            for rect, label in padded:
                if label:
                    size = min(7.0, rect.height * 0.75)
                    while size > 3.5 and pymupdf.get_text_length(label, fontsize=size) > rect.width - 1:
                        size -= 0.5
                    width = pymupdf.get_text_length(label, fontsize=size)
                    start = pymupdf.Point(
                        rect.x0 + max(0.5, (rect.width - width) / 2), rect.y0 + rect.height / 2 + size * 0.35
                    )
                    page.insert_text(start, label, fontsize=size, color=(0, 0, 0))

        pdf.set_metadata({})
        pdf.del_xml_metadata()
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Erasing pixels stores the page images uncompressed; pack them again
        # so the masked file is not several times the size of the original.
        if hasattr(pdf, "rewrite_images"):
            pdf.rewrite_images(quality=80, lossy=True, lossless=True)
        # A full rewrite: unreferenced objects are dropped, so removed content is gone from the file.
        pdf.save(str(out), garbage=4, deflate=True, clean=True)
    return out


def _rect(box) -> pymupdf.Rect:
    return pymupdf.Rect(box.x0, box.y0, box.x1, box.y1)


def _by_line(boxes: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Join the word boxes of one line into a single rectangle per line."""
    lines: list[pymupdf.Rect] = []
    for box in sorted(boxes, key=lambda r: (r.y0, r.x0)):
        if lines and min(box.y1, lines[-1].y1) - max(box.y0, lines[-1].y0) > 0.5 * min(box.height, lines[-1].height):
            lines[-1] |= box
        else:
            lines.append(pymupdf.Rect(box))
    return lines
