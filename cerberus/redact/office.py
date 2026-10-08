"""What the DOCX and PPTX writers share.

In both formats a paragraph is stored as "runs", one per stretch of
formatting, and a name can straddle two of them. Detection saw the joined
text. Writing a token back means deciding which characters of which run to
change, and leaving every other run, with its formatting, alone.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from ..extract.ir import Block, BlockType, Document
from .tokens import Tokenized

# File properties that name a person: replaced by a token, or emptied if no
# name was detected in them. Leaving an undetected value in place is not an option.
PERSON_PROPERTIES = ("author", "last_modified_by")
_GREY = 224


def spans_by_block(doc: Document, tokenized: list[Tokenized]) -> dict[str, list[tuple[int, int, str]]]:
    """For each block of this document: (start, end, token) for every finding outside pictures."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    for item in tokenized:
        f = item.finding
        if f.doc_id == doc.doc_id and f.location != "image":
            out.setdefault(f.block_id, []).append((f.start, f.end, item.token))
    return out


def apply_spans(
    pieces: list[str], spans: list[tuple[int, int, str]], fixed: frozenset[int] = frozenset()
) -> list[str]:
    """Replace character spans of the joined text, keeping the pieces separate.

    pieces are the texts of a paragraph's runs, in order. The token goes into
    the first piece the span touches; the rest of the span is deleted from
    whichever pieces hold it. Pieces a span does not touch are returned as they
    were, so their formatting is undisturbed.

    fixed lists pieces that cannot be changed (a tab, a line break). A span
    crossing one leaves it in place and clears the text on both sides.
    """
    starts, position = [], 0
    for piece in pieces:
        starts.append(position)
        position += len(piece)
    out = list(pieces)
    for start, end, token in sorted(spans, reverse=True):  # right to left: earlier offsets stay valid
        touched: list[tuple[int, int, int]] = []
        for index in range(len(pieces) - 1, -1, -1):
            lo, hi = starts[index], starts[index] + len(pieces[index])
            if hi <= start or lo >= end or lo == hi or index in fixed:
                continue
            cut_from, cut_to = max(start, lo) - lo, min(end, hi) - lo
            touched.append((index, cut_from, cut_to))
        if not touched:
            raise ValueError("a finding does not line up with the text of its paragraph")
        for index, cut_from, cut_to in touched:
            # Pieces are visited last to first, so the final entry is where the span begins.
            token_here = token if (index, cut_from, cut_to) == touched[-1] else ""
            out[index] = out[index][:cut_from] + token_here + out[index][cut_to:]
    return out


def property_value(block: Block, name: str, spans: dict[str, list[tuple[int, int, str]]]) -> str:
    """What a file property should hold in the masked copy."""
    found = spans.get(block.block_id, [])
    if name in PERSON_PROPERTIES:
        return " ".join(token for _, _, token in sorted(found)) if found else ""
    text = block.text
    for start, end, token in sorted(found, reverse=True):
        text = text[:start] + token + text[end:]
    return text


def core_property_blocks(doc: Document) -> dict[str, Block]:
    return {
        b.native_ref["core_property"]: b
        for b in doc.blocks
        if b.type is BlockType.METADATA and "core_property" in (b.native_ref or {})
    }


def placeholder(size: tuple[int, int] | None) -> bytes:
    """A grey PNG with the shape of the picture it replaces, labelled as masked."""
    width, height = size or (480, 240)
    scale = min(1.0, 640 / max(width, height))
    width, height = max(40, round(width * scale)), max(24, round(height * scale))
    image = Image.new("L", (width, height), _GREY)
    draw = ImageDraw.Draw(image)
    label = "[picture masked]"
    box = draw.textbbox((0, 0), label)
    if box[2] < width and box[3] < height:
        draw.text(((width - box[2]) / 2, (height - box[3]) / 2), label, fill=60)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
