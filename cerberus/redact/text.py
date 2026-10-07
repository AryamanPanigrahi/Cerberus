"""Redacted text: what the downstream model is allowed to read.

Every finding is replaced by its token, in place, so the text keeps its
structure (headings, lists, tables) and its meaning (who did what), and loses
only who the people are.

Three things are withheld entirely, not tokenised:

- Text read from inside a picture. OCR on pictures is too noisy to trust that
  every piece of PII in it was found, so none of it is passed on.
- Anything quarantined: a page or region the pipeline could not read reliably.
  Unreadable is not the same as empty.
- File properties, running headers and footers. They are scanned and reported,
  but they are not document content.

Tokenising is done right to left within a block, so replacing one span does
not shift the positions of the ones still to do.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

from ..eval import structure
from ..extract.ir import BlockType, Container, Document
from .tokens import Tokenized

PICTURE = "(picture withheld{detail})"
PAGE = "(page withheld: it could not be read reliably)"


def redact_document(doc: Document, tokenized: list[Tokenized]) -> Document:
    """A copy of the document with tokens in place of PII and withheld content removed.

    Block ids, order and parents are unchanged, so every block of the redacted
    copy can still be traced to its original. Word boxes are dropped: they
    described the original characters.
    """
    spans: dict[str, list[Tokenized]] = {}
    for item in tokenized:
        if item.finding.doc_id == doc.doc_id:
            spans.setdefault(item.finding.block_id, []).append(item)
    quarantined_pages = {p.number for p in doc.pages if p.quarantined}
    by_id = {b.block_id: b for b in doc.blocks}

    def in_picture(block) -> bool:
        if block.container is Container.IMAGE:
            return True
        parent = by_id.get(block.parent_id)
        while parent is not None:
            if parent.type is BlockType.IMAGE_REGION:
                return True
            parent = by_id.get(parent.parent_id)
        return False

    inside = Counter()  # picture block id -> PII items detected in it
    for block in doc.blocks:
        if block.type is not BlockType.IMAGE_REGION and in_picture(block):
            parent = by_id.get(block.parent_id)
            while parent is not None and parent.type is not BlockType.IMAGE_REGION:
                parent = by_id.get(parent.parent_id)
            if parent is not None:
                inside[parent.block_id] += len(spans.get(block.block_id, []))

    blocks, announced = [], set()
    for block in doc.blocks:
        text = block.text
        kind = block.type
        if block.page in quarantined_pages:
            # The first block of the page carries the notice; the rest are emptied.
            text = "" if block.page in announced or block.type is BlockType.SLIDE else PAGE
            if text:
                announced.add(block.page)
                kind = BlockType.PARAGRAPH
        elif block.type is BlockType.IMAGE_REGION:
            count = inside[block.block_id]
            detail = f": {count} item{'s' if count != 1 else ''} of PII detected inside" if count else ""
            text, kind = PICTURE.format(detail=detail), BlockType.PARAGRAPH
        elif in_picture(block) or block.quarantined:
            text = ""
        else:
            for item in sorted(spans.get(block.block_id, []), key=lambda t: -t.finding.start):
                text = text[: item.finding.start] + item.token + text[item.finding.end :]
        blocks.append(replace(block, text=text, type=kind, words=[]))
    return Document(doc.doc_id, doc.name, doc.format, list(doc.pages), blocks, dict(doc.versions))


def redacted_text(doc: Document, tokenized: list[Tokenized]) -> str:
    """The redacted document as text, one section per page, slide or top-level section."""
    clean = redact_document(doc, tokenized)
    label = {"pdf": "page", "pptx": "slide"}.get(doc.format, "section")
    parts = []
    for unit, blocks in sorted(structure.units(clean).items()):
        body = structure.to_markdown(clean, blocks).strip()
        if body:
            parts.append(f"<!-- {label} {unit} -->\n{body}")
    return "\n\n".join(parts) + "\n"
