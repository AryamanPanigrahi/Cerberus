"""PDF extractor: a PDF -> block IR.

Each page is handled on its own, because a PDF can mix typed and scanned pages:

- A page with a text layer is read directly (native).
- A page without one is rendered to an image and OCR'd. The words go through
  layout analysis to rebuild headings, paragraphs, lists and tables.
- Framed figures on a scanned page are cut out and OCR'd again, enlarged,
  because their print is too small to read at page resolution. Their text is
  marked as image content. A figure that still reads badly is quarantined.

Every page records which route it took (the "which pages needed OCR" report)
and, for OCR, the mean confidence. A page whose confidence is below the
threshold is quarantined: its findings are still reported, but its text must
not be passed downstream.

All positions are stored in PDF points, whatever resolution the page was
rendered at, so a mask can be drawn straight onto the page.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from statistics import median

import numpy as np
import pymupdf
from PIL import Image

from . import figures, layout, ocr
from .ir import BBox, Block, BlockType, Container, Document, Page, Source, Unit, Word, doc_id_from_bytes

# Starting values, to be calibrated against labelled pages.
OCR_DPI = 300  # render resolution; small print needs about this much to be legible
MIN_NATIVE_CHARS = 50  # fewer characters than this in the text layer: treat as scanned
MIN_PAGE_CONFIDENCE = 0.70  # mean OCR confidence below this quarantines the page
MIN_FIGURE_CONFIDENCE = 0.60  # ...and below this, a figure
_MARGIN = 0.06  # running headers and footers live in the top and bottom 6% of the page
_INFO_KEYS = ("title", "author", "subject", "keywords", "creator", "producer")


def extract_pdf(path: str | Path, dpi: int = OCR_DPI) -> Document:
    path = Path(path)
    doc = Document(
        doc_id_from_bytes(path.read_bytes()),
        path.name,
        "pdf",
        versions={"pymupdf": pymupdf.__version__},
    )
    with pymupdf.open(str(path)) as pdf:
        if pdf.needs_pass:
            raise ValueError(f"{path.name}: encrypted PDF, cannot be inspected")
        _emit_metadata(doc, pdf)

        # Pass 1: read every page, so repeated headers and footers can be recognised.
        scanned: dict[int, list[layout.Line]] = {}
        framed: dict[int, list[_Figure]] = {}
        for page in pdf:
            number = page.number + 1
            info = Page(number, page.rect.width, page.rect.height, Unit.PT)
            doc.pages.append(info)
            if len(page.get_text("text").strip()) >= MIN_NATIVE_CHARS:
                continue
            info.source = Source.OCR
            doc.versions.setdefault("tesseract", ocr.engine_version())
            doc.versions.setdefault("ocr_dpi", str(dpi))
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
            words = ocr.ocr_image(image)
            gray = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
            frames = figures.find_frames(gray)
            framed[number] = [_read_figure(page, frame, dpi) for frame in frames]
            # What the page pass read inside a figure is replaced by the enlarged pass.
            words = [w for w in words if not any(_inside(w, frame) for frame in frames)]
            scanned[number] = layout.build_lines(words)
        furniture = _furniture(scanned, {p.number: p.height * dpi / 72 for p in doc.pages})

        # Pass 2: emit blocks page by page; headings stay open across pages.
        emitter = _Emitter(doc, 72 / dpi)
        for page in pdf:
            number = page.number + 1
            if number not in scanned:
                emitter.native_page(page, number)
                continue
            lines = scanned[number]
            words = [w for line in lines for w in line.words]
            info = doc.pages[number - 1]
            if words:
                weight = sum(len(w.text) for w in words)
                info.ocr_conf = sum(w.conf * len(w.text) for w in words) / weight
                if info.ocr_conf < MIN_PAGE_CONFIDENCE:
                    info.quarantined = True
                    info.reason = f"OCR confidence {info.ocr_conf:.2f} below {MIN_PAGE_CONFIDENCE}"
            emitter.scanned_page(lines, furniture.get(number, {}), framed[number], number)

    errors = doc.validate()
    if errors:
        raise ValueError(f"{path.name}: extraction broke IR invariants: {errors[:5]}")
    return doc


def _emit_metadata(doc: Document, pdf) -> None:
    """The info dictionary and the XMP packet: two separate copies of the file properties."""
    for key in _INFO_KEYS:
        value = (pdf.metadata or {}).get(key)
        if value:
            doc.add(BlockType.METADATA, value, native_ref={"info": key})
    xmp = pdf.get_xml_metadata()
    if not xmp:
        return
    try:
        root = ET.fromstring(xmp)
    except ET.ParseError:  # unreadable, but still in the file: scan it as one string
        doc.add(BlockType.METADATA, xmp, native_ref={"xmp": "raw"})
        return
    for element in root.iter():
        if len(element) == 0 and (element.text or "").strip():
            tag = element.tag.rsplit("}", 1)[-1]
            doc.add(BlockType.METADATA, element.text.strip(), native_ref={"xmp": tag})


class _Figure:
    def __init__(self, frame, lines: list[layout.Line]):
        self.frame = frame  # (x0, y0, x1, y1) in page pixels at the OCR resolution
        self.lines = lines  # words already mapped back to those page pixels
        self.y0 = frame[1]


def _inside(word: ocr.OcrWord, frame) -> bool:
    x, y = (word.x0 + word.x1) / 2, (word.y0 + word.y1) / 2
    return frame[0] <= x <= frame[2] and frame[1] <= y <= frame[3]


def _read_figure(page, frame, dpi: int) -> _Figure:
    """Cut the frame out at the scan's own resolution, enlarge it, and OCR it."""
    # Rendering at the embedded image's native resolution avoids resampling it twice.
    native = max(
        (i["width"] / (pymupdf.Rect(i["bbox"]).width / 72) for i in page.get_image_info()),
        default=dpi,
    )
    native = int(min(600, max(72, round(native))))
    clip = pymupdf.Rect(*frame) * (72 / dpi)
    pixmap = page.get_pixmap(dpi=native, colorspace=pymupdf.csGRAY, clip=clip)
    cutout = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
    image, scale, left, top = figures.prepare(cutout)

    k = dpi / native  # cut-out pixels -> page pixels
    words = [
        ocr.OcrWord(
            w.text,
            frame[0] + (left + w.x0 / scale) * k,
            frame[1] + (top + w.y0 / scale) * k,
            frame[0] + (left + w.x1 / scale) * k,
            frame[1] + (top + w.y1 / scale) * k,
            w.conf,
            w.line,
        )
        for w in _best_reading(image)
    ]
    return _Figure(frame, layout.build_lines(words))


def _best_reading(image: Image.Image) -> list[ocr.OcrWord]:
    """OCR a figure under several segmentation modes and keep the best reading.

    No single mode suits every figure: a form reads best as one block (6), a
    badge or card as a column (4), a diagram as scattered text (11). The best
    reading is the one with the most confidently read characters.
    """
    def score(words):
        return sum(w.conf * len(w.text) for w in words if w.conf >= 0.6)

    return max((ocr.ocr_image(image, psm=mode) for mode in (6, 4, 11)), key=score)


def _furniture(scanned: dict[int, list[layout.Line]], heights: dict[int, float]) -> dict:
    """Running headers and footers: lines in the page margin that recur at the
    same height on at least half the pages. Returns {page: {line id: block type}}.

    Position is compared, not text, because OCR reads faint footer text
    differently on every page.
    """
    marginal = []  # (page, line, position as a fraction of page height, type)
    for number, lines in scanned.items():
        for line in lines:
            y = (line.y0 + line.y1) / 2 / heights[number]
            if y < _MARGIN:
                marginal.append((number, line, y, BlockType.HEADER))
            elif y > 1 - _MARGIN:
                marginal.append((number, line, y, BlockType.FOOTER))
    needed = max(3, len(scanned) / 2)
    result: dict[int, dict[int, BlockType]] = {}
    for number, line, y, kind in marginal:
        pages = {n for n, _, other, k in marginal if k is kind and abs(other - y) <= 0.006}
        if len(pages) >= needed:
            result.setdefault(number, {})[id(line)] = kind
    return result


class _Emitter:
    def __init__(self, doc: Document, to_points: float):
        self.doc = doc
        self.k = to_points  # OCR pixels -> PDF points
        self.headings: list[tuple[int, Block]] = []  # open headings as (level, block)

    def _context(self) -> dict:
        return dict(
            parent_id=self.headings[-1][1].block_id if self.headings else None,
            section_path=tuple(" ".join(b.text.split()) for _, b in self.headings),
        )

    def _heading(self, level: int, text: str, **fields) -> None:
        while self.headings and self.headings[-1][0] >= level:
            self.headings.pop()
        block = self.doc.add(BlockType.HEADING, text, **self._context(), **fields)
        self.headings.append((level, block))

    # --- scanned pages ----------------------------------------------------

    def scanned_page(self, lines, furniture: dict, framed: list, page: int) -> None:
        body = [line for line in lines if id(line) not in furniture]
        marginal = [line for line in lines if id(line) in furniture]
        for line in marginal:
            if furniture[id(line)] is BlockType.HEADER:
                self.doc.add(BlockType.HEADER, page=page, **self._ocr_fields(line.words))
        regions = [*layout.analyse(body), *framed]
        for region in sorted(regions, key=lambda r: r.y0):
            if isinstance(region, _Figure):
                self._figure(region, page)
                continue
            if isinstance(region, layout.TableRegion):
                self._table(region, page)
                continue
            fields = self._ocr_fields([w for line in region.lines for w in line.words])
            if region.kind == "heading":
                self._heading(region.level, page=page, **fields)
            else:
                kind = BlockType.LIST_ITEM if region.kind == "list_item" else BlockType.PARAGRAPH
                self.doc.add(kind, page=page, **self._context(), **fields)
        for line in marginal:
            if furniture[id(line)] is BlockType.FOOTER:
                self.doc.add(BlockType.FOOTER, page=page, **self._ocr_fields(line.words))

    def _figure(self, figure: _Figure, page: int) -> None:
        """An image region, with one child block per line of text read inside it."""
        k = self.k
        words = [w for line in figure.lines for w in line.words]
        weight = sum(len(w.text) for w in words)
        conf = sum(w.conf * len(w.text) for w in words) / weight if words else None
        # Unreadable is not the same as empty: a figure we cannot read may still
        # hold personal data, so it is masked whole and kept from downstream.
        bad = conf is not None and conf < MIN_FIGURE_CONFIDENCE
        region = self.doc.add(
            BlockType.IMAGE_REGION, page=page, container=Container.IMAGE, source=Source.OCR,
            bbox=BBox(*(v * k for v in figure.frame), Unit.PT), ocr_conf=conf, quarantined=bad,
            **self._context(),
        )
        for line in figure.lines:
            self.doc.add(
                BlockType.PARAGRAPH, page=page, container=Container.IMAGE, quarantined=bad,
                parent_id=region.block_id, section_path=region.section_path,
                **self._ocr_fields(line.words),
            )

    def _table(self, region: layout.TableRegion, page: int) -> None:
        k = self.k
        box = BBox(
            region.columns[0][0] * k, region.rows[0][0] * k,
            region.columns[-1][1] * k, region.rows[-1][1] * k, Unit.PT,
        )
        table = self.doc.add(
            BlockType.TABLE, page=page, bbox=box, container=Container.TABLE,
            source=Source.OCR, **self._context(),
        )
        headers: dict[int, str] = {}
        for r, (cells, (y0, y1)) in enumerate(zip(region.cells, region.rows)):
            for c, (words, (x0, x1)) in enumerate(zip(cells, region.columns)):
                fields = self._ocr_fields(words)
                fields["bbox"] = BBox(x0 * k, y0 * k, x1 * k, y1 * k, Unit.PT)
                cell = self.doc.add(
                    BlockType.TABLE_CELL, page=page, container=Container.TABLE,
                    parent_id=table.block_id, section_path=table.section_path,
                    row=r, col=c, header_id=headers.get(c) if r else None, **fields,
                )
                if r == 0 and len(region.rows) > 1:
                    headers[c] = cell.block_id

    def _ocr_fields(self, words: list[ocr.OcrWord]) -> dict:
        """Block text for a run of OCR words, with each word's character span and box."""
        k, text, spans = self.k, "", []
        for previous, word in zip([None, *words], words):
            if previous is not None:
                # A word broken by a hyphen at a line end is rejoined; the wrap
                # shows as the next word starting further left.
                wrapped = word.x0 < previous.x0
                if not (wrapped and previous.text.endswith("-") and len(previous.text) > 1):
                    text += " "
            box = BBox(word.x0 * k, word.y0 * k, word.x1 * k, word.y1 * k, Unit.PT)
            spans.append(Word(len(text), len(text) + len(word.text), box, word.conf))
            text += word.text
        fields = dict(text=text, words=spans, source=Source.OCR)
        if words:
            fields["ocr_conf"] = sum(w.conf for w in words) / len(words)
            fields["bbox"] = BBox(
                min(w.x0 for w in words) * k, min(w.y0 for w in words) * k,
                max(w.x1 for w in words) * k, max(w.y1 for w in words) * k, Unit.PT,
            )
        return fields

    # --- native pages -----------------------------------------------------

    def native_page(self, page, number: int) -> None:
        blocks = page.get_text("dict", sort=True)["blocks"]
        sizes = [s["size"] for b in blocks for ln in b.get("lines", []) for s in ln["spans"]]
        body_size = median(sizes) if sizes else 0
        for b in blocks:
            box = BBox(*b["bbox"], Unit.PT)
            if b["type"] != 0:  # an embedded picture; its text is added by the OCR stage
                self.doc.add(
                    BlockType.IMAGE_REGION, page=number, bbox=box,
                    container=Container.IMAGE, **self._context(),
                )
                continue
            lines = ["".join(s["text"] for s in ln["spans"]).strip() for ln in b["lines"]]
            text = " ".join(line for line in lines if line)
            if not text:
                continue
            size = max(s["size"] for ln in b["lines"] for s in ln["spans"])
            if len(lines) == 1 and size >= 1.15 * body_size and len(text.split()) <= 14:
                numbered = layout._NUMBERED_HEADING.match(text)
                level = numbered.group(1).count(".") + 1 if numbered else 1
                self._heading(level, text, page=number, bbox=box)
            else:
                self.doc.add(BlockType.PARAGRAPH, text, page=number, bbox=box, **self._context())
