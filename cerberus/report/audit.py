"""The audit report: what PII was found, where, and why we believe it.

Three things are written, none of which contains an original value. A finding
is named by its token (`<PERSON_3>`), so the report can be read by an auditor
who is not cleared to see the data, and tied back to a value only by someone
holding the vault.

    audit.csv            one row per finding: the "transparent tags"
    review_queue.csv     one row per picture masked whole, with the reason: the
                         pictures a person has to look at, because the pipeline
                         could not show them safely by itself
    <file>.summary.md    per file: pages read by OCR, what was withheld,
                         counts by category and by location, counts per page
    exposure.md          across files: totals, people seen in more than one
                         file, identities seen only inside pictures

The report is still derived from the documents (page numbers, counts, section
titles), so it belongs in the output folder and is never committed.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

from ..extract.ir import BlockType, Document
from ..redact.tokens import Tokenized, image_only

LOCATIONS = ("body", "table", "prose", "image")
COLUMNS = [
    "file", "page", "section", "location", "entity", "subtype", "token", "detector", "rule", "confidence",
    "flags", "read_by", "block", "start", "end", "box",
]


def rows(docs: list[Document], tokenized: list[Tokenized]) -> list[dict]:
    """One row per finding, in file and reading order. No original values."""
    by_id = {doc.doc_id: doc for doc in docs}
    # A heading can itself hold PII ("Approvals: Jane Sample"), and headings are quoted in the section column.
    known = sorted({(t.finding.text, t.token) for t in tokenized if len(t.finding.text) >= 4},
                   key=lambda pair: -len(pair[0]))
    out = []
    for item in tokenized:
        f = item.finding
        doc = by_id[f.doc_id]
        block = doc.get(f.block_id)
        out.append({
            "file": doc.name, "page": f.page or "", "section": _tokenise(" > ".join(block.section_path), known),
            "location": f.location, "entity": f.entity, "subtype": f.subtype, "token": item.token,
            "detector": f.detector, "rule": f.rule, "confidence": round(f.confidence, 2),
            "flags": " ".join(f.flags), "read_by": block.source.value, "block": f.block_id,
            "start": f.start, "end": f.end, "box": _box(block.span_boxes(f.start, f.end) or [block.bbox]),
            "_order": (docs.index(doc), block.order, f.start),
        })
    out.sort(key=lambda row: row.pop("_order"))
    return out


def _tokenise(text: str, known: list[tuple[str, str]]) -> str:
    for value, token in known:
        if value.lower() in text.lower():
            text = re.sub(re.escape(value), lambda _: token, text, flags=re.IGNORECASE)
    return text


def _box(boxes: list) -> str:
    """The rectangle around a finding on its page: "(x0, y0)-(x1, y1) unit".

    Written with punctuation between the numbers, so that a row of coordinates
    can never read as a long number when the report is searched for values.
    """
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return ""
    corners = (min(b.x0 for b in boxes), min(b.y0 for b in boxes), max(b.x1 for b in boxes), max(b.y1 for b in boxes))
    x0, y0, x1, y1 = (f"{value:.0f}" for value in corners)
    return f"({x0}, {y0})-({x1}, {y1}) {boxes[0].unit.value}"


REVIEW_COLUMNS = ["file", "page", "picture", "reason", "engines", "agreement", "pii_found_inside", "box"]


def why_whole(block, pictures: str = "findings") -> str | None:
    """Why a picture is masked whole, or None if it is masked word by word."""
    ref = block.native_ref or {}
    if "unreadable" in ref:
        return "could not be opened or converted"
    if ref.get("fallback"):
        return ref["fallback"]
    if len(ref.get("engines", [])) < 2:
        return "read by one OCR engine only"
    if ref.get("trusted") and not block.quarantined:
        return "every picture is masked whole in this mode" if pictures == "whole" else None
    if ref.get("agreement") is not None:
        return f"the two OCR engines agree on only {ref['agreement']:.0%} of its text"
    return "read with low confidence"


def review_queue(docs: list[Document], tokenized: list[Tokenized], pictures: str = "findings") -> list[dict]:
    """The pictures a person must look at: every one masked whole, and why.

    Masking a picture whole is the safe choice and also an admission: the
    pipeline could not tell what was in it well enough to show it. The number
    of rows here, out of all pictures, is how much of the picture channel
    still needs human review.
    """
    inside = Counter()
    by_id = {doc.doc_id: doc for doc in docs}
    for item in tokenized:
        f = item.finding
        if f.location == "image" and f.doc_id in by_id:
            inside[by_id[f.doc_id].get(f.block_id).parent_id] += 1
    queue = []
    for doc in docs:
        regions = [b for b in doc.blocks if b.type is BlockType.IMAGE_REGION]
        for number, block in enumerate(regions, start=1):
            reason = why_whole(block, pictures)
            if reason is None:
                continue
            ref = block.native_ref or {}
            queue.append({
                "file": doc.name, "page": block.page or "", "picture": number, "reason": reason,
                "engines": "+".join(ref.get("engines", [])), "agreement": "" if ref.get("agreement") is None else ref["agreement"],
                "pii_found_inside": inside[block.block_id], "box": _box([block.bbox]),
            })
    return queue


def _table(headings: list[str], lines: list[list]) -> list[str]:
    out = ["| " + " | ".join(headings) + " |", "|" + "---|" * len(headings)]
    return out + ["| " + " | ".join(str(cell) for cell in line) + " |" for line in lines]


def _by_entity(items: list[Tokenized]) -> list[str]:
    """Instances and distinct identities per category, split by the four locations."""
    entities = Counter(t.finding.entity for t in items)
    lines = []
    for entity, total in sorted(entities.items(), key=lambda pair: (-pair[1], pair[0])):
        mine = [t for t in items if t.finding.entity == entity]
        where = Counter(t.finding.location for t in mine)
        lines.append([entity, total, len({t.token for t in mine}), *(where[name] for name in LOCATIONS)])
    where = Counter(t.finding.location for t in items)
    lines.append(["**All**", len(items), len({t.token for t in items}), *(where[name] for name in LOCATIONS)])
    return _table(["Category", "Instances", "Distinct", *(name.capitalize() for name in LOCATIONS)], lines)


def _ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 7] -> "1-3, 7"."""
    parts, numbers = [], sorted(numbers)
    for n in numbers:
        if parts and n == parts[-1][1] + 1:
            parts[-1][1] = n
        else:
            parts.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in parts) or "none"


def summary(doc: Document, tokenized: list[Tokenized], mode: str = "findings") -> str:
    """The per-file summary, as Markdown."""
    items = [t for t in tokenized if t.finding.doc_id == doc.doc_id]
    unit = "slide" if doc.format == "pptx" else "page"
    pictures = [b for b in doc.blocks if b.type is BlockType.IMAGE_REGION]
    lines = [f"# PII summary: {doc.name}", ""]

    facts = []
    if doc.pages:
        facts.append([f"{unit.capitalize()}s", len(doc.pages)])
        facts.append([f"{unit.capitalize()}s read by OCR", f"{len(doc.ocr_pages())} ({_ranges(doc.ocr_pages())})"])
        withheld = [p.number for p in doc.pages if p.quarantined]
        facts.append([f"{unit.capitalize()}s withheld whole (unreadable)", f"{len(withheld)} ({_ranges(withheld)})"])
    trusted = [b for b in pictures if why_whole(b, mode) is None]
    facts.append(["Pictures", len(pictures)])
    facts.append(["Pictures read by two OCR engines", sum(len((b.native_ref or {}).get("engines", [])) > 1 for b in pictures)])
    facts.append(["Pictures masked word by word", len(trusted)])
    facts.append(["Pictures masked whole and queued for human review", len(pictures) - len(trusted)])
    cover = Counter(entry[0] for b in trusted for entry in (b.native_ref or {}).get("cover", []))
    facts.append(["Words in trusted pictures the engines read differently (covered unless one read an ordinary word)", cover["unconfirmed"]])
    facts.append(["Covered inside trusted pictures: faces", cover["face"]])
    facts.append(["Covered inside trusted pictures: barcodes", cover["barcode"]])
    facts.append(["PII instances", len(items)])
    facts.append(["Distinct identities", len({t.token for t in items})])
    lines += _table(["", ""], facts) + [""]

    lines += ["## By category and location", ""] + _by_entity(items) + [""]
    lines += ["Body: a labelled field or short line. Table: a table cell. Prose: inside a sentence, with no field "
              "label. Image: read from a picture by OCR.", ""]

    notes = Counter()
    for t in items:
        f = t.finding
        if f.subtype:
            notes[f"sub-type: {f.subtype}"] += 1
        notes.update(f"flag: {flag}" for flag in f.flags)
        notes[f"found by: {f.detector}"] += 1
    lines += ["## How they were found", ""] + _table(["", "Instances"], sorted(notes.items())) + [""]

    if doc.pages:
        per_page: dict[int, Counter] = {}
        for t in items:
            if t.finding.page is not None:
                per_page.setdefault(t.finding.page, Counter())[t.finding.location] += 1
        outside = sum(t.finding.page is None for t in items)
        lines += [f"## By {unit}", ""]
        lines += _table([unit.capitalize(), "Instances", *(name.capitalize() for name in LOCATIONS)], [
            [number, sum(count.values()), *(count[name] for name in LOCATIONS)]
            for number, count in sorted(per_page.items())
        ])
        if outside:
            lines += ["", f"Not on a {unit} (file properties): {outside}"]
        lines.append("")
    return "\n".join(lines)


def exposure(
    docs: list[Document], tokenized: list[Tokenized], verified: bool | None = None, mode: str = "findings"
) -> str:
    """The report across all files, as Markdown."""
    names = {doc.doc_id: doc.name for doc in docs}
    lines = ["# PII exposure report", ""]
    if verified is not None:
        lines += ["Verification of the outputs: " + ("**passed** (re-scan and raw search, every output)."
                                                     if verified else "**FAILED**. Outputs were withheld."), ""]

    lines += ["## By file", ""] + _table(["File", "Instances", "Distinct", *(n.capitalize() for n in LOCATIONS)], [
        [doc.name, len(mine), len({t.token for t in mine}),
         *(sum(t.finding.location == n for t in mine) for n in LOCATIONS)]
        for doc in docs for mine in [[t for t in tokenized if t.finding.doc_id == doc.doc_id]]
    ]) + [""]
    lines += ["## By category, all files", ""] + _by_entity(tokenized) + [""]

    files: dict[str, set[str]] = {}
    for t in tokenized:
        files.setdefault(t.token, set()).add(names[t.finding.doc_id])
    shared = sorted((token, sorted(where)) for token, where in files.items() if len(where) > 1)
    lines += ["## Identities found in more than one file", ""]
    lines += _table(["Token", "Files"], [[f"`{token}`", ", ".join(where)] for token, where in shared]) if shared else ["None."]
    lines += ["", "One token stands for one individual or value in every file, so these can be followed across "
              "files without revealing who or what they are.", ""]

    hidden = image_only(tokenized)
    kinds = Counter(token.strip("<>").rsplit("_", 1)[0] for token in hidden)
    lines += ["## Identities seen only inside pictures", ""]
    lines += [f"{len(hidden)} of {len(files)} identities appear in no ordinary text, only in a picture. "
              "Reading text alone would not have found them.", ""]
    if hidden:
        lines += _table(["Category", "Identities"], sorted(kinds.items(), key=lambda pair: (-pair[1], pair[0]))) + [""]
    lines += ["Text inside a picture is read with less certainty than typed text. It is never passed downstream, "
              "and a picture is masked whole unless two OCR engines agreed on it. This list may be incomplete.", ""]

    queue = review_queue(docs, tokenized, mode)
    total = sum(1 for doc in docs for b in doc.blocks if b.type is BlockType.IMAGE_REGION)
    lines += ["## Pictures queued for human review", ""]
    lines += [f"{len(queue)} of {total} pictures were masked whole. Each is a picture the pipeline could not show "
              "safely by itself; a person should look at the original before it is relied on. They are listed in "
              "`review_queue.csv`.", ""]
    if queue:
        reasons = Counter(re.sub(r"\d+%", "too little", row["reason"]) for row in queue)
        lines += _table(["Reason", "Pictures"], sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))) + [""]
    return "\n".join(lines)


def write(
    folder: str | Path, docs: list[Document], tokenized: list[Tokenized], verified: bool | None = None,
    pictures: str = "findings",
) -> list[Path]:
    """Write the audit log, the review queue, a summary per file and the exposure report."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = [folder / "audit.csv"]
    with open(written[0], "w", encoding="utf-8-sig", newline="") as handle:  # the BOM makes Excel read it as UTF-8
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows(docs, tokenized))
    for doc in docs:
        written.append(folder / f"{Path(doc.name).stem}.summary.md")
        written[-1].write_text(summary(doc, tokenized, pictures), encoding="utf-8")
    written.append(folder / "exposure.md")
    written[-1].write_text(exposure(docs, tokenized, verified, pictures), encoding="utf-8")
    written.append(folder / "review_queue.csv")
    with open(written[-1], "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(review_queue(docs, tokenized, pictures))
    return written
