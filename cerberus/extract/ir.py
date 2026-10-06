"""Block intermediate representation (IR) shared by every extractor.

Each extractor (PDF, DOCX, PPTX) turns a file into one Document: a flat,
ordered list of Blocks. Detectors and redactors only ever see this shape, so
they are written once and stay format-agnostic.

Flat list + parent pointers, not a nested tree: detectors just iterate text
units, and the structure metric needs exactly two facts per block (its order
and its parent). A tree can be rebuilt from parent_id when needed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum


class BlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    SLIDE = "slide"
    GROUP = "group"  # shapes that belong together, e.g. one org-chart card
    TEXT_BOX = "text_box"
    IMAGE_REGION = "image_region"
    HEADER = "header"
    FOOTER = "footer"
    NOTES = "notes"
    METADATA = "metadata"


class Container(str, Enum):
    """Where a block physically sits. Whether a finding is in labelled text or
    unlabelled prose is decided per finding, not here: one paragraph can hold both."""

    BODY = "body"
    TABLE = "table"
    IMAGE = "image"


class Source(str, Enum):
    NATIVE = "native"  # characters stored in the file
    OCR = "ocr"  # characters read from pixels


class Unit(str, Enum):
    """Each format keeps its own ruler so masks can be written back without rounding."""

    PT = "pt"  # PDF points, 1/72 inch
    EMU = "emu"  # PowerPoint, 914400 per inch
    PX = "px"  # image pixels


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float
    unit: Unit


@dataclass(frozen=True)
class Word:
    """One OCR word: text[start:end] of its block, and where it sits on the image.

    This is the link from a detected character span back to a rectangle to mask.
    """

    start: int
    end: int
    bbox: BBox
    conf: float  # 0..1, engine-independent


@dataclass
class Block:
    block_id: str
    type: BlockType
    order: int  # reading order across the whole document, from 1
    text: str = ""  # exactly as extracted; all finding offsets index into this
    container: Container = Container.BODY
    source: Source = Source.NATIVE
    parent_id: str | None = None  # the containing block, not the previous one
    page: int | None = None  # page or slide number from 1; None for DOCX
    bbox: BBox | None = None
    ocr_conf: float | None = None
    words: list[Word] = field(default_factory=list)
    native_ref: dict | None = None  # format-specific locator for write-back
    row: int | None = None  # table cells only
    col: int | None = None
    header_id: str | None = None  # the column-header cell this cell sits under
    section_path: tuple[str, ...] = ()  # enclosing headings, outermost first
    quarantined: bool = False  # unreliable: report its findings, mask it whole, keep it from downstream

    def span_boxes(self, start: int, end: int) -> list[BBox]:
        """Boxes of every word overlapping text[start:end]."""
        return [w.bbox for w in self.words if w.start < end and w.end > start]

    @classmethod
    def from_dict(cls, d: dict) -> Block:
        d = dict(d)
        d["type"] = BlockType(d["type"])
        d["container"] = Container(d["container"])
        d["source"] = Source(d["source"])
        d["bbox"] = _bbox(d.get("bbox"))
        d["words"] = [
            Word(w["start"], w["end"], _bbox(w["bbox"]), w["conf"]) for w in d.get("words", [])
        ]
        d["section_path"] = tuple(d.get("section_path", ()))
        return cls(**d)


@dataclass
class Page:
    """One page or slide. Carries the per-page OCR report and the fail-closed flag."""

    number: int
    width: float
    height: float
    unit: Unit
    source: Source = Source.NATIVE
    ocr_conf: float | None = None
    quarantined: bool = False  # blocked from downstream; findings are still reported
    reason: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> Page:
        return cls(**{**d, "unit": Unit(d["unit"]), "source": Source(d["source"])})


@dataclass
class Document:
    doc_id: str
    name: str
    format: str  # "pdf", "docx" or "pptx"
    pages: list[Page] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)  # extractor / OCR engine versions

    def add(self, type: BlockType, text: str = "", **fields) -> Block:
        """Append a block, assigning the next order and a deterministic id.

        Ids depend only on file content and position, so a re-run on the same
        file produces the same ids and therefore the same findings.
        """
        order = len(self.blocks) + 1
        block = Block(f"{self.doc_id}/{order:05d}", type, order, text, **fields)
        self.blocks.append(block)
        return block

    def get(self, block_id: str) -> Block:
        return self.blocks[int(block_id.rsplit("/", 1)[1]) - 1]

    def children(self, block_id: str) -> list[Block]:
        return [b for b in self.blocks if b.parent_id == block_id]

    def ocr_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.source is Source.OCR]

    def validate(self) -> list[str]:
        """Return every broken invariant (empty list = sound).

        Extractors call this before handing a document on; a non-empty result
        means the file is quarantined rather than passed downstream.
        """
        errors: list[str] = []
        seen: dict[str, Block] = {}
        page_numbers = {p.number for p in self.pages}

        for i, b in enumerate(self.blocks, start=1):
            where = f"block {b.block_id}"
            if b.order != i:
                errors.append(f"{where}: order {b.order} at position {i}")
            if b.block_id in seen:
                errors.append(f"{where}: duplicate id")
            if b.parent_id is not None and b.parent_id not in seen:
                errors.append(f"{where}: parent {b.parent_id} missing or not earlier")
            if b.page is not None and page_numbers and b.page not in page_numbers:
                errors.append(f"{where}: page {b.page} not in document")
            if b.bbox is not None and (b.bbox.x0 > b.bbox.x1 or b.bbox.y0 > b.bbox.y1):
                errors.append(f"{where}: inverted bbox")

            if b.type is BlockType.TABLE_CELL:
                parent = seen.get(b.parent_id)
                if parent is None or parent.type is not BlockType.TABLE:
                    errors.append(f"{where}: cell parent is not a table")
                if b.row is None or b.col is None:
                    errors.append(f"{where}: cell without row/col")
                header = seen.get(b.header_id) if b.header_id else None
                if b.header_id and (header is None or header.type is not BlockType.TABLE_CELL):
                    errors.append(f"{where}: header {b.header_id} is not an earlier cell")

            # OCR text with no word boxes could be detected but never masked.
            if b.source is Source.OCR and b.text.strip() and not b.words:
                errors.append(f"{where}: OCR text without word boxes")
            prev_end = 0
            for w in b.words:
                if not (prev_end <= w.start < w.end <= len(b.text)):
                    errors.append(f"{where}: word span {w.start}:{w.end} out of order or range")
                if not 0.0 <= w.conf <= 1.0:
                    errors.append(f"{where}: word confidence {w.conf} outside 0..1")
                prev_end = max(prev_end, w.end)

            seen[b.block_id] = b
        return errors

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, s: str) -> Document:
        d = json.loads(s)
        d["pages"] = [Page.from_dict(p) for p in d["pages"]]
        d["blocks"] = [Block.from_dict(b) for b in d["blocks"]]
        return cls(**d)


def doc_id_from_bytes(data: bytes) -> str:
    """Content hash, so the id follows the file's content rather than its name or path."""
    return hashlib.sha256(data).hexdigest()[:12]


def _bbox(d: dict | None) -> BBox | None:
    return BBox(d["x0"], d["y0"], d["x1"], d["y1"], Unit(d["unit"])) if d else None
