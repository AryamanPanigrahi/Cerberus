"""A finding: one piece of PII located in one block.

This is the "transparent tag" the brief asks for. Every field needed to
explain a finding, reproduce it and mask it travels with it: where it is
(file, page, block, character span), what it is (entity, subtype), and why we
believe it (which detector and rule fired, with what confidence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..extract.ir import Block, Container, Document


@dataclass(frozen=True)
class Finding:
    doc_id: str
    block_id: str
    start: int  # character span in the block's text
    end: int
    text: str  # the matched characters; stays local, never written to committed files
    entity: str  # EMAIL, PHONE, STAFF_ID, SSN, TAX_ID, PAN, PESEL, PASSPORT, CARD, DOB
    detector: str  # "rules", later "ner", "dictionary"
    rule: str  # the specific rule or model that fired
    confidence: float  # 0..1
    subtype: str = ""  # "partial", "masked", ...
    flags: tuple[str, ...] = ()  # e.g. "keyword", "test_range", "ocr", "quarantined"
    page: int | None = None
    location: str = "body"  # body, table, prose or image (the four classes of exercise prompt 2)


_FIELD_LABEL = re.compile(r"[A-Za-z][\w /.()'-]{0,30}:\s*$")
_SHORT_LINE = 12  # words; a block this short is a field or a caption, not a sentence


def location_of(block: Block, start: int) -> str:
    """Where a finding sits, in the four classes the audit report distinguishes.

    The picture and the table are decided by the block. Outside those, the
    question is whether the value sits in a field or in a sentence:

    - body: a short line, or a value straight after a "Label:". Field-based
      extraction finds these.
    - prose: a value inside running text. A sentence may well say what the
      value is ("her SSN was ..."), but there is no field to read it from.
    """
    if block.container is Container.IMAGE:
        return "image"
    if block.container is Container.TABLE:
        return "table"
    in_field = len(block.text.split()) <= _SHORT_LINE or _FIELD_LABEL.search(
        block.text[max(0, start - 40) : start]
    )
    return "body" if in_field else "prose"


def make(doc: Document, block: Block, start: int, end: int, **fields) -> Finding:
    return Finding(doc.doc_id, block.block_id, start, end, block.text[start:end], page=block.page, **fields)
