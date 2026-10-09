"""What the DOCX and PPTX writers share.

In both formats a paragraph is stored as "runs", one per stretch of
formatting, and a name can straddle two of them. Detection saw the joined
text. Writing a token back means deciding which characters of which run to
change, and leaving every other run, with its formatting, alone.
"""

from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

from ..detect import allowlist
from ..extract.ir import Block, BlockType, Container, Document, Source
from .tokens import Tokenized

# File properties that name a person: replaced by a token, or emptied if no
# name was detected in them. Leaving an undetected value in place is not an option.
PERSON_PROPERTIES = ("author", "last_modified_by")
_GREY = 224
_PICTURE_PAD = 2  # pixels added round a rectangle painted inside a picture


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


def common_words(doc: Document) -> frozenset[str]:
    """Words this document writes in small letters, in text that can be relied on.

    A word seen in small letters in the body of the document ("payment",
    "access", "effective") is an ordinary word. Names are not written that
    way. The list is taken from the document itself, so it needs no
    dictionary and fits whatever the document is about. Words inside
    pictures, hidden properties and words the OCR was unsure of are left out,
    as is anything that is not a plain word (an e-mail address holds a name
    in small letters).
    """
    words: set[str] = set()
    for block in doc.blocks:
        if block.container is Container.IMAGE or block.type is BlockType.METADATA or not block.text:
            continue
        if block.source is Source.OCR and block.words:
            tokens = [block.text[w.start : w.end] for w in block.words if w.conf >= 0.8]
        else:
            tokens = block.text.split()
        for token in tokens:
            token = token.strip(".,;:!?()[]\"'“”‘’")
            if len(token) > 1 and token.islower() and token.replace("-", "").isalpha():
                words.update(part for part in token.split("-") if len(part) > 1)
    return frozenset(words)


_SHORT_CODE = re.compile(r"[\W_]*[A-Z]{2,5}[-./]?\d{1,4}[\W_]*")  # a reference such as a ticket or risk number


def _harmless(text: str, common: frozenset[str]) -> bool:
    """Could this word, as read, be left visible?

    Yes for an ordinary word, a short number, a short reference code,
    punctuation, and a scrap of one or two letters (what OCR makes of an icon
    or a coloured square; covering it hides nothing). An ordinary word read
    with a letter wrong is still an ordinary word, so a near match counts.
    """
    if "@" in text or sum(c.isdigit() for c in text) >= 5:
        return False
    if _SHORT_CODE.fullmatch(text):
        return True
    parts = [part.lower() for part in re.findall(r"[A-Za-z]+", text) if len(part) > 2]
    return all(part in common or _near_common(part, common) for part in parts)


def _near_common(word: str, common: frozenset[str]) -> bool:
    """Is the word one wrong letter from an ordinary word (two, for a long word)?"""
    if len(word) < 5:
        return False  # too short: one letter turns most short words into another
    allowed = 2 if len(word) >= 9 else 1
    return any(abs(len(known) - len(word)) <= allowed and _edits(word, known, allowed) <= allowed for known in common)


def _edits(a: str, b: str, limit: int) -> int:
    """Letters to insert, delete or replace to turn a into b; limit + 1 if more."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _segments(block: Block, columns: tuple[float, ...] = ()) -> list[tuple[int, int]]:
    """A line read from a picture, cut where the words are far apart.

    A line across a table or a chart joins cells that have nothing to do with
    each other. Shapes are looked for within one stretch of neighbouring
    words, so a row of five numbers is not taken for a phone number.

    columns are x positions where a column of the picture starts (see
    _columns). A long value can end close to the next column, so a word that
    starts on a column is the start of a cell even after a small gap.
    """
    words = sorted(block.words, key=lambda w: w.start)
    if not words:
        return [(0, len(block.text))]
    heights = sorted(w.bbox.y1 - w.bbox.y0 for w in words)
    height = heights[len(heights) // 2]
    pieces = [[words[0].start, words[0].end]]
    for previous, word in zip(words, words[1:]):
        gap = word.bbox.x0 - previous.bbox.x1
        on_column = gap > 0.5 * height and any(abs(word.bbox.x0 - x) <= 0.5 * height for x in columns)
        if gap > 2.0 * height or on_column or word.bbox.y0 > previous.bbox.y1:
            pieces.append([word.start, word.end])
        else:
            pieces[-1][1] = word.end
    return [(a, b) for a, b in pieces]


def _columns(lines: list[Block]) -> tuple[float, ...]:
    """Where the columns of a table in a picture start: x positions at which
    at least three lines begin a new stretch of words after a wide gap."""
    starts: list[float] = []
    for line in lines:
        words = sorted(line.words, key=lambda w: w.start)
        for begin, _ in _segments(line)[1:]:
            starts += [w.bbox.x0 for w in words if w.start == begin]
    if not starts or not lines:
        return ()
    sizes = sorted(w.bbox.y1 - w.bbox.y0 for line in lines for w in line.words)
    near = 0.5 * sizes[len(sizes) // 2]
    columns: list[list[float]] = []
    for x in sorted(starts):
        if columns and x - columns[-1][-1] <= near:
            columns[-1].append(x)
        else:
            columns.append([x])
    return tuple(sum(group) / len(group) for group in columns if len(group) >= 3)


_CELL_WORDS = 6  # a stretch of neighbouring words this short is a table cell or a field, not a sentence
_INITIALS = re.compile(r"[\W_]*(?:[A-Z]\.?){1,3}[\W_]*")  # "J.", "J.Q.", "JQ."
_SURNAME = re.compile(r"[\W_]*[A-Z][a-z]{2,}(?:[-'’][A-Za-z][a-z]+)*[\W_]*")  # one capitalised word


def _possible_surname(text: str, common: frozenset[str]) -> bool:
    """A capitalised word that is not an ordinary word of this document.

    A surname alone in a table cell has no shape a rule can see, and a model
    reading a line of garbled cells may not report it. What marks it is only
    that the document never writes it in small letters.
    """
    return bool(_SURNAME.fullmatch(text)) and not _harmless(text, common)


def _initials(text: str) -> bool:
    return bool(_INITIALS.fullmatch(text)) and any(c.isalpha() for c in text)


def beside(box, mask, reach: float = 1.5) -> bool:
    """Is the box on the same line as the mask and within `reach` text heights of it?"""
    height = box[3] - box[1]
    down = min(box[3], mask[3]) - max(box[1], mask[1])
    gap = max(mask[0] - box[2], box[0] - mask[2], 0.0)
    return height > 0 and down >= 0.5 * height and gap < reach * height


def residue_like(text: str) -> bool:
    """Initials written with a full stop: what is left of a name when only its surname was covered."""
    return _initials(text) and "." in text


def picture_boxes(doc: Document, tokenized: list[Tokenized], pictures: str = "findings") -> dict[str, list | None]:
    """For every picture: the rectangles to cover in it, or None to mask it whole.

    Keyed by the image region's block id. Rectangles are in the units the
    region's words are stored in (points on a PDF page, pixels of the picture
    in a DOCX or PPTX).

    A picture is masked word by word only if extraction marked it trusted: two
    OCR engines read it and agreed on enough of it. Then a word is marked if it is:

    - part of a finding;
    - part of anything shaped like personal data in either engine's reading,
      detected or not (allowlist.shaped_like_pii);
    - a capitalised word the document never writes in small letters (a
      surname alone in a cell has no other mark);
    - a word the engines read differently, unless one of them read an
      ordinary word there. Two readings that differ and are both nonsense
      are what a name or an address under a stamp looks like.

    What is covered is then wider than the marked words:

    - a table cell or a label's value (a short stretch of neighbouring words)
      is covered whole if any word in it is marked. Covering the surname and
      leaving the initials beside it is not masking a name;
    - in a sentence, marked words next to each other are covered as one
      rectangle;
    - initials left standing next to a rectangle are taken into it, whichever
      engine or line they were read on;
    - every rectangle gets a margin, so the edge of a letter does not show.

    Faces and barcodes are covered as well. Anything less certain is masked
    whole, as is everything when pictures="whole".

    Covering every word the engines disagreed on was tried first. On a scanned
    form it hid over a hundred harmless words, most of the text. "Ordinary
    word" is decided from the document itself: see common_words.
    """
    common = common_words(doc)
    found: dict[str, list] = {}
    for item in tokenized:
        if item.finding.doc_id == doc.doc_id and item.finding.location == "image":
            found.setdefault(item.finding.block_id, []).append(item.finding)

    plans: dict[str, list | None] = {}
    for region in doc.blocks:
        if region.type is not BlockType.IMAGE_REGION:
            continue
        ref = region.native_ref or {}
        if pictures == "whole" or region.quarantined or not ref.get("trusted"):
            plans[region.block_id] = None
            continue
        lines = doc.children(region.block_id)
        if any(not line.span_boxes(f.start, f.end) for line in lines for f in found.get(line.block_id, [])):
            plans[region.block_id] = None  # found, but cannot be placed: cover everything
            continue
        doubtful = {tuple(round(v, 2) for v in entry[1:]) for entry in ref.get("cover", []) if entry[0] == "unconfirmed"}
        read = [  # every word read in this picture: (engine, text, box)
            ((line.native_ref or {}).get("engine"), line.text[w.start : w.end], w.bbox) for line in lines for w in line.words
        ]

        def ordinary_to_the_other(engine, box) -> bool:
            """Did the other engine read an ordinary word in this place?"""
            return any(
                other != engine and len(re.sub(r"[^A-Za-z]", "", theirs)) >= 3 and _harmless(theirs, common)
                and _shared(box, place) > 0.5
                for other, theirs, place in read
            )

        def unread(engine, text, box) -> bool:
            """Did the engines differ here, with no ordinary word in either reading?"""
            if (round(box.x0, 2), round(box.y0, 2), round(box.x1, 2), round(box.y1, 2)) not in doubtful:
                return False
            return not _harmless(text, common) and not ordinary_to_the_other(engine, box)

        columns = {
            engine: _columns([line for line in lines if (line.native_ref or {}).get("engine") == engine])
            for engine in {(line.native_ref or {}).get("engine") for line in lines}
        }

        boxes: list[tuple[float, float, float, float]] = []
        for line in lines:
            words = sorted(line.words, key=lambda w: w.start)
            if not words:
                continue
            engine = (line.native_ref or {}).get("engine")
            marked: set[int] = set()

            def mark(start: int, stop: int) -> None:
                marked.update(i for i, w in enumerate(words) if w.start < stop and w.end > start)

            cells = _segments(line, columns.get(engine, ()))
            for begin, finish in cells:
                for start, stop in allowlist.shaped_like_pii(line.text[begin:finish], common):
                    mark(begin + start, begin + stop)
            for start, stop in allowlist.shaped_like_pii(line.text, common, initials_only=True):
                mark(start, stop)
            for f in found.get(line.block_id, []):
                mark(f.start, f.end)
            for i, w in enumerate(words):
                text = line.text[w.start : w.end]
                surname = _possible_surname(text, common) and not ordinary_to_the_other(engine, w.bbox)
                if surname or unread(engine, text, w.bbox):
                    marked.add(i)

            for begin, finish in cells:
                members = [i for i, w in enumerate(words) if begin <= w.start < finish]
                hit = [i for i in members if i in marked]
                if not hit:
                    continue
                if len(members) <= _CELL_WORDS:
                    groups = [members]  # a cell or a field: all of it
                else:  # a sentence: each run of marked neighbours, with initials standing before it
                    groups = []
                    for i in hit:
                        if groups and groups[-1][-1] == i - 1:
                            groups[-1].append(i)
                        else:
                            groups.append([i])
                    for group in groups:
                        while group[0] - 1 in members and _initials(line.text[words[group[0] - 1].start : words[group[0] - 1].end]):
                            group.insert(0, group[0] - 1)
                for group in groups:
                    parts = [words[i].bbox for i in group]
                    boxes.append((min(b.x0 for b in parts), min(b.y0 for b in parts),
                                  max(b.x1 for b in parts), max(b.y1 for b in parts)))

        # Initials read on another line, or by the other engine, beside something already covered.
        for _ in range(2):
            for _, text, place in read:
                box = (place.x0, place.y0, place.x1, place.y1)
                if _initials(text) and any(beside(box, mask, reach=1.0) for mask in boxes) and box not in boxes:
                    boxes.append(box)

        boxes = [(x0 - 0.25 * (y1 - y0), y0 - 0.12 * (y1 - y0), x1 + 0.25 * (y1 - y0), y1 + 0.12 * (y1 - y0))
                 for x0, y0, x1, y1 in boxes]
        plans[region.block_id] = _joined(boxes) + [tuple(e[1:]) for e in ref.get("cover", []) if e[0] != "unconfirmed"]
    return plans


def _joined(boxes: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Rectangles on one line that touch or overlap, as one rectangle each."""
    out: list[list[float]] = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        for other in out:
            down = min(box[3], other[3]) - max(box[1], other[1])
            if down >= 0.6 * min(box[3] - box[1], other[3] - other[1]) and box[0] <= other[2] and other[0] <= box[2]:
                other[:] = [min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]), max(box[3], other[3])]
                break
        else:
            out.append(list(box))
    return [tuple(box) for box in out]


def residue(words: list[tuple[str, object, float]], masks: list, min_confidence: float = 0.5) -> int:
    """How many readable scraps are left beside the masks of one masked picture.

    words are (text, box, confidence) as read from the masked picture; masks
    are the rectangles that were covered in it. Two things count: initials
    with a full stop standing next to a mask, and a word that a mask cuts
    through (part inside, part outside), which is a value only half covered.
    """
    count = 0
    for text, place, confidence in words:
        if confidence < min_confidence or sum(c.isalnum() for c in text) == 0:
            continue
        box = (place.x0, place.y0, place.x1, place.y1)
        area = max(1e-9, (box[2] - box[0]) * (box[3] - box[1]))
        inside = max(
            (max(0.0, min(box[2], m[2]) - max(box[0], m[0])) * max(0.0, min(box[3], m[3]) - max(box[1], m[1])) / area
             for m in masks), default=0.0,
        )
        if inside >= 0.6:
            continue  # under a mask: whatever the OCR made of the grey, it is not text
        cut = inside >= 0.15 and sum(c.isalnum() for c in text) >= 2
        if cut or (residue_like(text) and any(beside(box, m, reach=1.0) for m in masks)):
            count += 1
    return count


def _shared(a, b) -> float:
    """Share of the smaller box that lies inside the other."""
    across, down = min(a.x1, b.x1) - max(a.x0, b.x0), min(a.y1, b.y1) - max(a.y0, b.y0)
    if across <= 0 or down <= 0:
        return 0.0
    return across * down / max(1e-9, min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0)))


def pictures_by_key(doc: Document, plans: dict[str, list | None], key) -> dict:
    """The same plans, keyed the way a writer finds a picture in the file.

    key(native_ref) names the stored image. One image can be shown in several
    places; it is written once, so the plans for all its uses are combined:
    every rectangle from any of them, or whole if any of them says whole.
    Each value is (rectangles or None, picture size).
    """
    out: dict = {}
    for block in doc.blocks:
        if block.type is not BlockType.IMAGE_REGION:
            continue
        ref = block.native_ref or {}
        name = key(ref)
        boxes, size = plans.get(block.block_id), tuple(ref.get("pixels") or ()) or None
        if name in out:
            earlier = out[name][0]
            boxes = None if boxes is None or earlier is None else earlier + boxes
        out[name] = (boxes, size)
    return out


def covered_picture(blob: bytes, content_type: str | None, boxes: list | None, size=None) -> bytes:
    """The picture as a new PNG with the given rectangles painted over, or a placeholder.

    The picture is rebuilt from its pixels even when there is nothing to cover,
    so nothing stored in the original image file (camera or author details,
    an embedded thumbnail, text in a vector drawing) is carried into the copy.
    """
    if boxes is None:
        return placeholder(size)
    from ..extract import images  # here: the extractors are not needed just to import the writers

    try:
        picture = images.open_picture(blob, content_type)
    except ValueError:
        return placeholder(size)
    draw = ImageDraw.Draw(picture)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle((x0 - _PICTURE_PAD, y0 - _PICTURE_PAD, x1 + _PICTURE_PAD, y1 + _PICTURE_PAD), fill=(_GREY,) * 3)
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


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
