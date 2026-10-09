"""NER detector: names and addresses, found by a local model.

A name has no fixed shape, so no rule can find it. GLiNER is a small
transformer that is given a text and a list of labels ("person", "address")
and returns the spans that match each label. It runs entirely on this machine;
the weights are a local folder and no document text leaves the process.

A model reads meaning from context, and much of a document's PII sits where
there is none: a table cell holding nothing but a name. So context is put back
before the model sees the text:

- the cells of a table row are joined into one line, each with its column
  header in front ("Name: ... | Emp ID: ...");
- a hidden file property is given its field name ("author: ...").

Spans are mapped back to the block and characters they came from, and a span
that falls in the added context rather than in the block is discarded.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from ..extract.ir import Block, BlockType, Document, Source
from . import allowlist
from .finding import Finding, location_of, make
from .merge import resolve_overlaps

# A local folder beside the package, found wherever the code is run from; override with CERBERUS_NER_MODEL.
MODEL_PATH = str(Path(__file__).resolve().parents[2] / "models" / "gliner-pii-small-v1.0")
LABELS = {"person": "PERSON", "address": "ADDRESS"}  # what the model is asked for -> our entity
# Also asked for, then thrown away. The model gives each span one label, so
# offering it "organization" and "job title" stops a company or a role being
# labelled a person for want of anything better.
DISTRACTORS = ("organization", "job title")
# Deliberately low. The model is unsure about names in table cells and about
# unfamiliar names, and scores many real ones between 0.2 and 0.5. Recall is
# bought with the low threshold; precision is restored afterwards by the shape
# and word filters in allowlist.py. To be calibrated against labelled pages.
THRESHOLD = 0.2
_MAX_WORDS = 160  # the model reads a few hundred tokens at a time; longer text is split
_OVERLAP = 30  # words shared by neighbouring pieces, so a name on a boundary is seen whole
_BATCH = 16
_NO_TEXT = {BlockType.TABLE, BlockType.SLIDE, BlockType.GROUP}

# texts, labels, threshold -> for each text, spans as {"start", "end", "label", "score"}
Predict = Callable[[list[str], list[str], float], list[list[dict]]]


@dataclass
class _Piece:
    """One text handed to the model, with where each stretch of it came from."""

    text: str
    sources: list[tuple[int, int, Block, int]]  # (start in piece, end in piece, block, start in block)


def load_predictor(model_path: str | None = None) -> tuple[Predict, str]:
    """Load GLiNER from a local folder. Returns the predict function and the model's name."""
    path = Path(model_path or os.environ.get("CERBERUS_NER_MODEL") or MODEL_PATH)
    if not path.is_dir():
        raise RuntimeError(f"NER model folder not found: {path} (set CERBERUS_NER_MODEL)")
    return _load(str(path))


@lru_cache(maxsize=2)
def _load(path: str) -> tuple[Predict, str]:
    """Loading takes many seconds, and one run scans several times (the
    documents, then every output again to verify it), so it is done once."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never reach for the network at run time
    from gliner import GLiNER  # imported here: it is slow to load and only this detector needs it

    model = GLiNER.from_pretrained(path, local_files_only=True)
    model.eval()

    def predict(texts: list[str], labels: list[str], threshold: float) -> list[list[dict]]:
        return model.inference(texts, labels, threshold=threshold, batch_size=_BATCH)

    return predict, Path(path).name


def detect(
    docs: list[Document],
    predict: Predict | None = None,
    model_name: str = "custom",
    threshold: float = THRESHOLD,
) -> list[Finding]:
    """Find names and addresses in every document.

    predict can be supplied (tests do, to avoid loading a model); otherwise the
    local GLiNER model is loaded.
    """
    if predict is None:
        predict, model_name = load_predictor()
    findings: list[Finding] = []
    for doc in docs:
        pieces = list(_pieces(doc))
        results = predict([p.text for p in pieces], [*LABELS, *DISTRACTORS], threshold)
        for piece, spans in zip(pieces, results):
            for span in spans:
                if span["label"] not in LABELS:
                    continue
                located = _locate(piece, span["start"], span["end"])
                if located is None:
                    continue
                block, whole_start, whole_end = located
                entity = LABELS[span["label"]]
                plausible = allowlist.plausible_person if entity == "PERSON" else allowlist.plausible_address
                flags = (("ocr",) if block.source is Source.OCR else ()) + (
                    ("quarantined",) if block.quarantined else ()
                )
                # A line break inside a block separates entries (two names in one
                # table cell). A person span reaching across one is two people.
                parts = [(whole_start, whole_end)]
                if entity == "PERSON":
                    parts = [
                        (whole_start + m.start(), whole_start + m.end())
                        for m in re.finditer(r"[^\n]+", block.text[whole_start:whole_end])
                    ]
                for start, end in parts:
                    start, end = _trim(block.text, start, end)
                    if entity == "PERSON":
                        start = _complete_left(block.text, start, end)
                    if not plausible(block.text[start:end]):
                        continue
                    findings.append(
                        make(
                            doc, block, start, end,
                            entity=entity, detector="ner", rule=model_name,
                            confidence=round(float(span["score"]), 3), flags=flags,
                            location=location_of(block, start),
                        )
                    )
    return resolve_overlaps(findings)


def _pieces(doc: Document):
    """Everything the model should read, with context added and long text split."""
    by_id = {b.block_id: b for b in doc.blocks}
    rows: dict[tuple[str, int], list[Block]] = {}
    for block in doc.blocks:
        if block.type in _NO_TEXT or not any(c.isalpha() for c in block.text):
            continue
        if block.type is BlockType.TABLE_CELL:
            rows.setdefault((block.parent_id, block.row), []).append(block)
        elif block.type is BlockType.METADATA:
            ref = block.native_ref or {}
            name = next((str(v) for k, v in ref.items() if k.endswith("property") or k in ("info", "xmp")), "")
            yield from _split(_join([(f"{name.replace('_', ' ')}: " if name else "", block)]))
        else:
            yield from _split(_join([("", block)]))
    for cells in rows.values():
        parts = []
        for cell in sorted(cells, key=lambda c: c.col):
            header = by_id[cell.header_id].text if cell.header_id else ""
            parts.append((f"{' '.join(header.split())}: " if header.strip() else "", cell))
        yield from _split(_join(parts, separator=" | "))


def _join(parts: list[tuple[str, Block]], separator: str = "") -> _Piece:
    text, sources = "", []
    for index, (prefix, block) in enumerate(parts):
        text += (separator if index else "") + prefix
        sources.append((len(text), len(text) + len(block.text), block, 0))
        text += block.text
    return _Piece(text, sources)


def _split(piece: _Piece):
    """Cut a long piece into overlapping windows of whole words."""
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", piece.text)]
    if len(words) <= _MAX_WORDS:
        yield piece
        return
    step = _MAX_WORDS - _OVERLAP
    for first in range(0, len(words), step):
        window = words[first : first + _MAX_WORDS]
        lo, hi = window[0][0], window[-1][1]
        sources = [
            (max(a, lo) - lo, min(b, hi) - lo, block, offset + max(a, lo) - a)
            for a, b, block, offset in piece.sources
            if a < hi and b > lo
        ]
        yield _Piece(piece.text[lo:hi], sources)
        if first + _MAX_WORDS >= len(words):
            break


def _locate(piece: _Piece, start: int, end: int) -> tuple[Block, int, int] | None:
    """Map a span in a piece back to one block. A span in added context, or one
    straddling two cells, belongs to no block and is dropped."""
    for a, b, block, offset in piece.sources:
        if a <= start and end <= b:
            return block, offset + start - a, offset + end - a
    return None


_STARTS_WITH_INITIAL = re.compile(r"[A-Z]\.?\s")
_WORD_BEFORE = re.compile(r"([^\W\d_]+(?:[-'’][^\W\d_]+)*)\s$")


def _complete_left(text: str, start: int, end: int) -> int:
    """Take in a first name the model left outside the span.

    The model sometimes returns "Q. Sample" from "Jane Q. Sample". A span that
    begins with an initial, straight after a capitalised word that is not a
    title or label, is a name missing its first word. Leaving that word
    unmasked would show half the name.
    """
    if not _STARTS_WITH_INITIAL.match(text[start:end]):
        return start
    before = _WORD_BEFORE.search(text[:start])
    if before is None:
        return start
    word = before.group(1)
    if len(word) > 2 and allowlist._is_name_word(word) and word.lower() not in allowlist.NOT_NAME_WORDS:
        return before.start(1)
    return start


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and not text[start].isalnum():
        start += 1
    while end > start and not (text[end - 1].isalnum() or text[end - 1] == "."):
        end -= 1
    return start, end
