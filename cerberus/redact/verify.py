"""Verification pass: prove the outputs are clean before anyone gets them.

Detection answers "what PII is in the original?". This answers a different
question: "is any of it still in the output?". Redaction can fail silently (a
token written one character out, a second copy of the text left untouched, a
file property forgotten), so the pipeline does not trust its own output. It
checks it, twice, in ways that do not share a weakness:

1. Re-scan. The output is put through extraction and the detectors again, as
   if it were a new document. Anything they find is a problem.
2. Raw search. Every original value the pipeline replaced is searched for in
   the raw contents of the output: every part of a DOCX or PPTX, the text,
   properties and object streams of a PDF. This does not use the extractor,
   so it sees the places the extractor does not read.

If either check finds anything the outputs are withheld and the run fails.

A picture masked word by word gets a third look. The re-scan uses the same
OCR and the same detectors that produced the masks, so it shares their blind
spot: a name it could not recognise before, it cannot recognise now. What it
can still see is the shape of a bad mask: initials left standing beside a
covered surname, or a word a mask cuts through. Either sends that picture
back to be masked whole.

What this cannot do: find PII that was never detected and so was never in the
list of values. It proves "everything we found is gone", not "there was
nothing else to find". That second claim is what the labelled evaluation is for.
"""

from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pymupdf

from ..detect.finding import Finding
from ..extract import extract
from ..extract.ir import BlockType, Container, Document
from .tokens import Tokenized

TOKEN = re.compile(r"<[A-Z][A-Z_]*_\d+>")
# Shorter values ("the last four digits") would match stray numbers in a file's
# own markup. They are still covered by the re-scan, which reads them in context.
_MIN_LENGTH = 6

# The notices the redactors write in place of withheld content.
_NOTICE = re.compile(r"\[(?:figure|picture|page) masked\]|\((?:picture|page) withheld[^)]*\)")

Detect = Callable[[list[Document]], list[Finding]]


class VerificationError(RuntimeError):
    """Raised when an output still contains PII. The outputs must not be released."""


@dataclass(frozen=True)
class Problem:
    output: str  # which output file
    check: str  # "rescan" or "raw"
    where: str  # page, block or part
    what: str  # entity or token; never the value itself, so this can be shown and logged
    page: int | None = None  # for a re-scan finding: the page or slide it is on
    in_picture: bool = False  # for a re-scan finding: was it read from a picture?
    picture: tuple[int, int] | None = None  # which picture: (its place among the file's pictures, how many there are)


def known_values(tokenized: list[Tokenized]) -> dict[str, str]:
    """Every original value that was replaced, mapped to its token."""
    values: dict[str, str] = {}
    for item in tokenized:
        value = " ".join(item.finding.text.split()).lower()
        if len(value) >= _MIN_LENGTH:
            values.setdefault(value, item.token)
    return values


def find_values(text: str, values: dict[str, str]) -> list[str]:
    """Tokens whose original value occurs in this text.

    Compared in lower case with white space collapsed, and again with all
    white space removed, so a value broken over two lines or two runs is found.
    """
    flat = " ".join(text.lower().split())
    packed = flat.replace(" ", "")
    return sorted({token for value, token in values.items() if value in flat or value.replace(" ", "") in packed})


def check_text(
    name: str, text: str, tokenized: list[Tokenized], detect: Detect, blocks: Document | None = None
) -> list[Problem]:
    """Both checks on a redacted text.

    The text is searched for every original value, and its lines are re-scanned
    with the rules, which judge a value by its shape and the words beside it.

    The model is different: its answer depends on how the text is laid out, and
    it gives a few new opinions every time the same words are presented another
    way. So the model re-scan reads the redacted document in block form
    (`blocks`), the form detection used and already made stable, not the text
    file's own line-by-line layout. Without `blocks`, the lines are all it has.
    """
    from ..detect import rules  # here, not at the top: detect imports nothing from redact, and stays that way

    problems = [Problem(name, "raw", "text", token) for token in find_values(text, known_values(tokenized))]
    lines = Document("verify", name, "text")
    for line in text.splitlines():
        if line.strip():
            lines.add(BlockType.PARAGRAPH, line)
    if blocks is None:
        return problems + _rescan(name, lines, detect)
    return problems + _rescan(name, lines, rules.detect) + _rescan(name, blocks, detect)


def check_file(
    path: str | Path, tokenized: list[Tokenized], detect: Detect, original: Document | None = None, masks: dict | None = None
) -> list[Problem]:
    """Both checks on a masked PDF, DOCX or PPTX.

    original and masks (the extraction of the source file, and what
    office.picture_boxes decided for it) add the look for scraps left beside
    the masks inside pictures.
    """
    path = Path(path)
    values = known_values(tokenized)
    problems = [
        Problem(path.name, "raw", where, token)
        for where, text in _raw_contents(path)
        for token in find_values(text, values)
    ]
    again = extract(path)
    problems += _rescan(path.name, again, detect)
    if original is not None and masks:
        problems += _scraps(path.name, again, original, masks)
    return problems


def _scraps(name: str, again: Document, original: Document, masks: dict) -> list[Problem]:
    """Pictures of the masked file in which something readable is left beside a mask."""
    from .office import common_words, residue  # here: office imports nothing from verify, and stays that way

    common = common_words(original)

    def words_of(blocks) -> list:
        return [(b.text[w.start : w.end], w.bbox, w.conf) for b in blocks for w in b.words]

    before = [b for b in original.blocks if b.type is BlockType.IMAGE_REGION]
    after = [b for b in again.blocks if b.type is BlockType.IMAGE_REGION]
    problems = []
    if original.format == "pdf":
        # Positions are page positions, so a picture is found by its page.
        for page in sorted({b.page for b in before if masks.get(b.block_id)}):
            boxes = [box for b in before if b.page == page for box in masks.get(b.block_id) or []]
            read = [b for b in again.blocks if b.page == page and b.container is Container.IMAGE]
            if residue(words_of(read), boxes, common=common):
                problems.append(Problem(name, "residue", f"page {page}, image", "scrap beside a mask", page, True))
    elif len(before) == len(after):
        # Positions are pixels of the picture, which masking does not resize: found by its place in the file.
        for index, (old, new) in enumerate(zip(before, after)):
            if masks.get(old.block_id) and residue(words_of(again.children(new.block_id)), masks[old.block_id], common=common):
                problems.append(Problem(name, "residue", f"picture {index + 1}", "scrap beside a mask", old.page, True,
                                        (index, len(after))))
    return problems


def ensure_clean(problems: list[Problem]) -> None:
    if problems:
        lines = [f"{p.output}: {p.check} check, {p.where}: {p.what}" for p in problems[:20]]
        more = f" (and {len(problems) - 20} more)" if len(problems) > 20 else ""
        raise VerificationError("PII remains in the output; nothing was released.\n" + "\n".join(lines) + more)


def _rescan(name: str, doc: Document, detect: Detect) -> list[Problem]:
    blocks = {b.block_id: b for b in doc.blocks}
    pictures = [b.block_id for b in doc.blocks if b.type is BlockType.IMAGE_REGION]
    problems = []
    for f in detect([doc]):
        block = blocks[f.block_id]
        # Our own tokens and notices are not PII, whatever a detector makes of them.
        if any(m.start() < f.end and f.start < m.end() for m in TOKEN.finditer(block.text)):
            continue
        if _NOTICE.fullmatch(block.text.strip()):
            continue
        where = f"page {f.page}, {f.location}" if f.page else f.location
        inside = block.parent_id if f.location == "image" else None
        picture = (pictures.index(inside), len(pictures)) if inside in pictures else None
        problems.append(Problem(name, "rescan", where, f.entity, f.page, f.location == "image", picture))
    return problems


def _raw_contents(path: Path):
    """(where, text) for everything in the file that could hold a value."""
    if path.suffix.lower() == ".pdf":
        yield from _pdf_contents(path)
        return
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".emf", ".wmf", ".bin")):
                continue  # pixels: covered by the re-scan, which reads pictures with OCR
            data = archive.read(name).decode("utf-8", "ignore")
            yield name, html.unescape(data)  # as stored: includes attributes such as an author
            # With the markup removed: text split across runs reads as one string again.
            yield name, html.unescape(re.sub(r"<[^>]+>", "", data))


def _pdf_contents(path: Path):
    with pymupdf.open(str(path)) as pdf:
        yield "file properties", " ".join(str(v) for v in (pdf.metadata or {}).values() if v)
        yield "XMP", pdf.get_xml_metadata() or ""
        for page in pdf:
            yield f"page {page.number + 1} text layer", page.get_text()
            for annotation in page.annots() or []:
                yield f"page {page.number + 1} annotation", " ".join(str(v) for v in annotation.info.values())
        for xref in range(1, pdf.xref_length()):
            yield f"object {xref}", pdf.xref_object(xref, compressed=True)
            if pdf.xref_is_stream(xref) and pdf.xref_get_key(xref, "Subtype")[1] != "/Image":
                yield f"object {xref} stream", (pdf.xref_stream(xref) or b"").decode("latin-1", "ignore")
