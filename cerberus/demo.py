"""What the demonstration notebook shows, kept out of the notebook.

Each function returns HTML (or text) for one view of a run, so the notebook's
cells stay one line long and the views can be tested like any other code.

Two kinds of view:

- of a run just made (`counts`, `findings`, `text_views`): built from the
  documents and findings in memory;
- of a run already on disk (`saved_report`, `pages`, `pictures`, `text_file`):
  built from the output folder alone, so the long run on the full files can be
  made beforehand and only read during the demonstration.

Only `findings(..., values=True)` and `text_views` show original values. Use
them on the made-up sample files, never on real documents in front of people.
"""

from __future__ import annotations

import base64
import csv
import io
import time
import zipfile
from collections import Counter
from html import escape
from pathlib import Path

from . import pipeline
from .extract.ir import BlockType, Container, Document
from .redact.text import redact_document
from .redact.tokens import Tokenized
from .report import audit

_STYLE = (
    "<style>.cb table{border-collapse:collapse;font-size:13px;margin:6px 0}"
    ".cb th,.cb td{border:1px solid #c8ccd4;padding:3px 8px;text-align:left;vertical-align:top}"
    ".cb th{background:#eef0f4}.cb td.n{text-align:right}"
    ".cb mark{background:#ffd6e7;padding:0 2px}.cb .tok{background:#d9f2d9;padding:0 2px;font-family:monospace}"
    ".cb .pair{display:flex;gap:14px;align-items:flex-start}.cb .pair>div{flex:1;min-width:0}"
    ".cb .cap{font-size:12px;color:#5b6472;margin-bottom:3px}"
    ".cb img{max-width:100%;border:1px solid #c8ccd4}.cb pre{white-space:pre-wrap;font-size:12px}</style>"
)
SUPPORTED = (".pdf", ".docx", ".pptx")


def files_in(folder: str | Path) -> list[Path]:
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in SUPPORTED)


def run(
    folder: str | Path, out: str | Path, pictures: str = "findings", fast: bool = False
) -> tuple[pipeline.Result, float]:
    """Run the whole pipeline on every supported file in folder. Returns the result and the seconds taken.

    fast=True reads pictures with one OCR engine, not two. That is several
    times quicker on a file with many pictures, and it changes one thing:
    every picture is masked whole and queued for review, because masking a
    picture word by word needs two engines to agree on it. Text, tables and
    the redacted text for the LLM are the same in both modes.
    """
    import os

    before = os.environ.get("CERBERUS_OCR")
    if fast:
        os.environ["CERBERUS_OCR"] = "tesseract"
    started = time.perf_counter()
    try:
        result = pipeline.run(files_in(folder), out, pictures=pictures)
    finally:
        if fast:
            os.environ.pop("CERBERUS_OCR", None) if before is None else os.environ.update(CERBERUS_OCR=before)
    return result, time.perf_counter() - started


def save_uploads(uploaded, folder: str | Path) -> list[Path]:
    """Write the files chosen in a notebook upload button into folder; other file types are left out.

    uploaded is the `.value` of an ipywidgets FileUpload: a sequence of
    entries with a name and the file's bytes (older versions: a dict by name).
    The folder is emptied of earlier documents first, so a run processes
    exactly what was just uploaded.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    entries = uploaded.values() if isinstance(uploaded, dict) else uploaded
    chosen = [(Path(entry["name"]).name, bytes(entry["content"])) for entry in entries]
    chosen = [(name, data) for name, data in chosen if Path(name).suffix.lower() in SUPPORTED]
    if chosen:
        for old in files_in(folder):
            old.unlink()
    written = []
    for name, data in chosen:
        (folder / name).write_bytes(data)
        written.append(folder / name)
    return written


def _table(headings: list[str], rows: list[list], numeric: frozenset[int] = frozenset()) -> str:
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headings)
    body = "".join(
        "<tr>" + "".join(f"<td{' class=n' if i in numeric else ''}>{cell}</td>" for i, cell in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f"{_STYLE}<div class=cb><table><tr>{head}</tr>{body}</table></div>"


def counts(tokenized: list[Tokenized]) -> str:
    """Instances and distinct identities by category, split by the four locations."""
    rows = []
    for entity, total in Counter(t.finding.entity for t in tokenized).most_common():
        mine = [t for t in tokenized if t.finding.entity == entity]
        where = Counter(t.finding.location for t in mine)
        rows.append([entity, total, len({t.token for t in mine}), *(where[name] or "" for name in audit.LOCATIONS)])
    where = Counter(t.finding.location for t in tokenized)
    rows.append(["<b>All</b>", len(tokenized), len({t.token for t in tokenized}), *(where[n] for n in audit.LOCATIONS)])
    return _table(["Category", "Instances", "Distinct", *(n.capitalize() for n in audit.LOCATIONS)], rows,
                  frozenset(range(1, 7)))


def findings(docs: list[Document], tokenized: list[Tokenized], limit: int = 30, values: bool = False) -> str:
    """The audit log's rows: one per finding, with where it is and why it was reported.

    values=True adds the original value. Only for made-up documents.
    """
    originals = {(t.finding.block_id, t.finding.start): t.finding.text for t in tokenized}
    columns = ["file", "page", "location", "entity", "token", "detector", "rule", "confidence", "flags"]
    rows = []
    for row in audit.rows(docs, tokenized)[:limit]:
        cells = [escape(str(row[c])) for c in columns]
        if values:
            cells.insert(5, f"<mark>{escape(originals[(row['block'], row['start'])])}</mark>")
        rows.append(cells)
    headings = columns[:5] + (["original value"] if values else []) + columns[5:]
    more = len(tokenized) - limit
    note = f"<div class=cb><div class=cap>and {more} more rows in audit.csv</div></div>" if more > 0 else ""
    return _table(headings, rows) + note


def text_views(doc: Document, tokenized: list[Tokenized], limit: int = 12) -> str:
    """Original text beside redacted text, block by block. Shows original values: made-up documents only."""
    clean = {b.block_id: b for b in redact_document(doc, tokenized).blocks}
    spans: dict[str, list[Tokenized]] = {}
    for item in tokenized:
        if item.finding.doc_id == doc.doc_id:
            spans.setdefault(item.finding.block_id, []).append(item)
    rows = []
    for block in doc.blocks:
        if not block.text.strip() or block.type in (BlockType.SLIDE, BlockType.TABLE, BlockType.GROUP):
            continue
        hidden = block.type in (BlockType.METADATA, BlockType.HEADER, BlockType.FOOTER)
        marked, at = "", 0
        for item in sorted(spans.get(block.block_id, []), key=lambda t: t.finding.start):
            f = item.finding
            marked += escape(block.text[at : f.start]) + f"<mark>{escape(block.text[f.start : f.end])}</mark>"
            at = f.end
        marked += escape(block.text[at:])
        # File properties and running headers are searched for PII but never passed on.
        shown = "" if hidden else escape(clean[block.block_id].text)
        shown = shown or "<i>withheld</i>"
        for token in {t.token for t in spans.get(block.block_id, [])}:
            shown = shown.replace(escape(token), f"<span class=tok>{escape(token)}</span>")
        where = "picture" if block.container is Container.IMAGE else block.type.value
        where = "file property" if block.type is BlockType.METADATA else where
        rows.append([escape(where), marked.replace("\n", "<br>"), shown.replace("\n", "<br>")])
        if len(rows) == limit:
            break
    return _table(["Block", "As extracted", "As passed to the LLM"], rows)


# --- views of a run on disk ---------------------------------------------------


def saved_report(out: str | Path, name: str = "exposure.md") -> str:
    """One of the report files of a finished run, as Markdown text (tokens and counts, no values)."""
    return (Path(out) / "report" / name).read_text(encoding="utf-8-sig")


def saved_counts(out: str | Path) -> str:
    """Category by location, for every file of a finished run, from its audit log."""
    with open(Path(out) / "report" / "audit.csv", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    table = []
    for name in sorted({r["file"] for r in rows}):
        mine = [r for r in rows if r["file"] == name]
        where = Counter(r["location"] for r in mine)
        table.append([escape(name), len(mine), len({r["token"] for r in mine}), *(where[n] or "" for n in audit.LOCATIONS)])
    return _table(["File", "Instances", "Distinct", *(n.capitalize() for n in audit.LOCATIONS)], table,
                  frozenset(range(1, 7)))


def text_file(out: str | Path, stem: str, lines: tuple[int, int] = (0, 40)) -> str:
    """Part of the redacted text of one file, as it would be handed to the LLM."""
    text = (Path(out) / "redacted" / f"{stem}.md").read_text(encoding="utf-8").splitlines()
    return f"{_STYLE}<div class=cb><pre>{escape(chr(10).join(text[lines[0] : lines[1]]))}</pre></div>"


def _png(data: bytes) -> str:
    return f"<img src='data:image/png;base64,{base64.b64encode(data).decode()}'>"


def _pair(left: str, right: str, left_caption: str, right_caption: str) -> str:
    return (f"{_STYLE}<div class=cb><div class=pair><div><div class=cap>{escape(left_caption)}</div>{left}</div>"
            f"<div><div class=cap>{escape(right_caption)}</div>{right}</div></div></div>")


def pages(original: str | Path, masked: str | Path, page: int, dpi: int = 110, clip=None) -> str:
    """One PDF page before and after masking, side by side. The left side shows the original.

    clip=(x0, y0, x1, y1) as fractions of the page shows only that part, larger.
    """
    import pymupdf

    def render(path) -> bytes:
        with pymupdf.open(str(path)) as pdf:
            sheet = pdf[page - 1]
            area = None
            if clip:
                r = sheet.rect
                area = pymupdf.Rect(r.width * clip[0], r.height * clip[1], r.width * clip[2], r.height * clip[3])
            return sheet.get_pixmap(dpi=dpi, clip=area).tobytes("png")

    return _pair(_png(render(original)), _png(render(masked)), f"original, page {page}", f"masked, page {page}")


def pictures(original: str | Path, masked: str | Path, number: int = 1, part: str | None = None) -> str:
    """The n-th picture of a DOCX or PPTX before and after masking. The left side shows the original.

    Pictures are counted in the order the document shows them. part names the
    part to look in; by default the body of a DOCX or the first slide of a PPTX.
    """
    return _pair(_png(_nth_picture(original, number, part)), _png(_nth_picture(masked, number, part)),
                 f"original, picture {number}", f"masked, picture {number}")


def _nth_picture(path: str | Path, number: int, part: str | None) -> bytes:
    import posixpath
    import re

    from PIL import Image

    from .extract import images

    part = part or ("word/document.xml" if str(path).lower().endswith(".docx") else "ppt/slides/slide1.xml")
    folder, name = posixpath.split(part)
    with zipfile.ZipFile(path) as archive:
        used = re.findall(r'r:embed="([^"]+)"', archive.read(part).decode("utf-8"))
        links = dict(re.findall(r'Id="([^"]+)"[^>]*?Target="([^"]+)"', archive.read(f"{folder}/_rels/{name}.rels").decode("utf-8")))
        links.update((b, a) for a, b in re.findall(r'Target="([^"]+)"[^>]*?Id="([^"]+)"', archive.read(f"{folder}/_rels/{name}.rels").decode("utf-8")))
        if not 1 <= number <= len(used):
            raise ValueError(f"{Path(path).name} shows {len(used)} pictures in {part}")
        target = posixpath.normpath(posixpath.join(folder, links[used[number - 1]]))
        blob = archive.read(target)
    kinds = {".emf": "image/x-emf", ".wmf": "image/x-wmf", ".svg": "image/svg+xml"}
    picture = images.open_picture(blob, kinds.get(posixpath.splitext(target)[1].lower()))
    picture.thumbnail((1100, 1100), Image.LANCZOS)
    out = io.BytesIO()
    picture.save(out, format="PNG")
    return out.getvalue()


def evaluation(folder: str | Path) -> str:
    """The aggregate recall tables written by cerberus.eval.recall (counts only)."""
    folder = Path(folder)
    parts = []
    for name in ("summary.txt", "ocr/ocr_comparison.txt"):
        if (folder / name).exists():
            parts.append(f"== {name} ==\n" + (folder / name).read_text(encoding="utf-8"))
    return "\n\n".join(parts) or f"no evaluation found in {folder}"
