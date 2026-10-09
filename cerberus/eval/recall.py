"""Recall against ground truth: of the PII a person found, how much did we find?

Runs extraction and detection on one document and compares the findings on the
labelled pages with the labels (see `ground_truth.py`).

A label and a finding are paired when they are on the same page and their text
is the same after lower-casing and collapsing white space. Pairing is counted:
a value labelled three times on a page needs three findings. What is left is
paired more loosely, and reported apart so the strict figure stays honest:

    exact    same text
    near     within two character edits: OCR altered the value, we still found it
    partial  one text contains the other: found, but the edges differ
    garbled  found in the right place but read badly: more than two characters
             wrong, yet at least four in five the same. Counted apart from the
             three above and never folded into them, so those keep their meaning
    missed   nothing on the page pairs with it

Two questions are kept separate, because they have different cures:

    extraction coverage   is the labelled value in the page's extracted text at all?
    recall                given the text, did a detector report it?

A value the OCR never read cannot be detected, whatever the detectors do.

Everything that holds a value is written to the output folder (gitignored).
Only counts are printed.

Run:  python -m cerberus.eval.recall <document> --labels <workbook.xlsx> --sheet <tab>

With --compare-ocr the document is processed twice, once reading pictures with
Tesseract alone and once with Tesseract and docTR together, and the two are
set side by side. Add --location image to score only what is inside pictures.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from ..detect.finding import Finding
from ..extract.ir import Document
from .ground_truth import Label, load, normalise

NEAR = 2  # character edits allowed for a "near" pairing
GARBLED = 0.8  # share of characters alike for a "garbled" pairing
_CONTEXT = 60  # characters of extracted text shown either side of a value
# The labellers' names for our entity types, where they differ.
_SAME_TYPE = {"EMPLOYEE_ID": "STAFF_ID"}


@dataclass
class Outcome:
    label: Label
    how: str = "missed"  # exact, near, partial, garbled or missed
    finding: Finding | None = None
    distance: int = 0
    in_text: str = "no"  # is the value in the page's extracted text: exact, near or no
    context: str = ""  # the extracted text around it, if it is there


def distance(a: str, b: str, limit: int = NEAR) -> int:
    """Character edits (insert, delete, replace) to turn a into b; limit + 1 if more."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return min(previous[-1], limit + 1)


def locate(value: str, text: str, limit: int = NEAR) -> tuple[int, int, int] | None:
    """Where value occurs in text, exactly or within `limit` edits: (start, end, edits).

    The approximate search is the same table as `distance` with one change:
    starting anywhere in the text costs nothing, so the best cell in the last
    row is the best match of the whole value against any stretch of the text.
    """
    at = text.find(value)
    if at >= 0:
        return at, at + len(value), 0
    if not value or not text:
        return None
    previous = list(range(len(value) + 1))
    best, best_end = limit + 1, -1
    for end, character in enumerate(text, 1):
        current = [0]
        for j, wanted in enumerate(value, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (wanted != character)))
        if current[-1] < best:
            best, best_end = current[-1], end
        previous = current
    if best > limit:
        return None
    return max(0, best_end - len(value)), best_end, best


def page_texts(doc: Document) -> dict[int, str]:
    """All text extracted from each page, pictures included, normalised."""
    pages: dict[int, list[str]] = {}
    for block in doc.blocks:
        if block.page is not None and block.text.strip():
            pages.setdefault(block.page, []).append(block.text)
    return {page: normalise(" ".join(texts)) for page, texts in pages.items()}


def compare(labels: list[Label], findings: list[Finding], texts: dict[int, str]) -> tuple[list[Outcome], list[Finding]]:
    """Pair labels with findings page by page. Returns the outcomes and the findings left over."""
    outcomes = [Outcome(label) for label in labels]
    left = list(findings)

    def pair(outcome: Outcome, finding: Finding, how: str, edits: int = 0) -> None:
        outcome.how, outcome.finding, outcome.distance = how, finding, edits
        left.remove(finding)

    # Strictest first, so a loose pairing never takes a finding an exact one needs.
    for outcome in outcomes:
        wanted = normalise(outcome.label.text)
        for finding in left:
            if finding.page == outcome.label.page and normalise(finding.text) == wanted:
                pair(outcome, finding, "exact")
                break

    close = []
    for index, outcome in enumerate(outcomes):
        if outcome.how != "missed":
            continue
        for finding in left:
            if finding.page == outcome.label.page:
                edits = distance(normalise(outcome.label.text), normalise(finding.text))
                if edits <= NEAR:
                    close.append((edits, index, id(finding), finding))
    for edits, index, _, finding in sorted(close, key=lambda item: item[:3]):  # closest pairs first
        if outcomes[index].how == "missed" and any(finding is f for f in left):
            pair(outcomes[index], finding, "near", edits)

    for outcome in outcomes:
        if outcome.how != "missed":
            continue
        wanted = normalise(outcome.label.text)
        for finding in left:
            got = normalise(finding.text)
            if finding.page == outcome.label.page and len(got) >= 4 and (got in wanted or wanted in got):
                pair(outcome, finding, "partial")
                break

    # What is left: a finding that is plainly the labelled value, read with more than two errors.
    alike = []
    for index, outcome in enumerate(outcomes):
        if outcome.how != "missed":
            continue
        wanted = normalise(outcome.label.text).replace(" ", "")
        for finding in left:
            if finding.page == outcome.label.page:
                ratio = SequenceMatcher(None, wanted, normalise(finding.text).replace(" ", ""), autojunk=False).ratio()
                if ratio >= GARBLED:
                    alike.append((-ratio, index, id(finding), finding))
    for _, index, _, finding in sorted(alike, key=lambda item: item[:3]):
        if outcomes[index].how == "missed" and any(finding is f for f in left):
            pair(outcomes[index], finding, "garbled")

    for outcome in outcomes:
        text = texts.get(outcome.label.page, "")
        place = locate(normalise(outcome.label.text), text)
        if place:
            start, end, edits = place
            outcome.in_text = "exact" if edits == 0 else "near"
            outcome.context = f"{text[max(0, start - _CONTEXT):start]}[[{text[start:end]}]]{text[end:end + _CONTEXT]}"
    return outcomes, left


# --- reporting -------------------------------------------------------------


def _share(part: int, whole: int) -> str:
    return f"{part / whole:6.1%}" if whole else "     -"


def summary(outcomes: list[Outcome], unmatched: list[Finding], findings: int) -> str:
    """The aggregate report. Counts and category names only: safe to print."""
    lines = []

    def recall_table(title: str, key) -> None:
        lines.append(f"\nRecall {title}")
        lines.append(f"  {'':<14}{'labels':>7}{'exact':>7}{'near':>6}{'partial':>8}{'garbled':>8}{'missed':>7}"
                     f"{'strict':>9}{'+near':>8}{'+partial':>9}{'+garbled':>9}")
        groups: dict[str, list[Outcome]] = {}
        for outcome in outcomes:
            groups.setdefault(key(outcome), []).append(outcome)
        for name, group in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
            how = Counter(o.how for o in group)
            n = len(group)
            lines.append(
                f"  {name or '?':<14}{n:>7}{how['exact']:>7}{how['near']:>6}{how['partial']:>8}{how['garbled']:>8}"
                f"{how['missed']:>7}{_share(how['exact'], n):>9}{_share(how['exact'] + how['near'], n):>8}"
                f"{_share(how['exact'] + how['near'] + how['partial'], n):>9}{_share(n - how['missed'], n):>9}"
            )

    recall_table("overall", lambda o: "all")
    recall_table("by type", lambda o: o.label.type)
    recall_table("by location", lambda o: o.label.location)
    recall_table("by page", lambda o: f"page {o.label.page}")

    lines.append("\nExtraction coverage (labelled value present in the page's extracted text)")
    lines.append(f"  {'':<14}{'labels':>7}{'exact':>7}{'near':>6}{'absent':>7}{'exact':>9}{'+near':>8}")
    groups = {"all": outcomes}
    for outcome in outcomes:
        groups.setdefault(outcome.label.location or "?", []).append(outcome)
    for name, group in groups.items():
        seen = Counter(o.in_text for o in group)
        n = len(group)
        lines.append(f"  {name:<14}{n:>7}{seen['exact']:>7}{seen['near']:>6}{seen['no']:>7}"
                     f"{_share(seen['exact'], n):>9}{_share(seen['exact'] + seen['near'], n):>8}")

    missed = [o for o in outcomes if o.how == "missed"]
    lines.append("\nWhy labels were missed")
    lines.append(f"  not in the extracted text (an extraction or OCR loss): {sum(o.in_text == 'no' for o in missed)}")
    lines.append(f"  in the text, altered by OCR, not detected:             {sum(o.in_text == 'near' for o in missed)}")
    lines.append(f"  in the text as labelled, not detected:                 {sum(o.in_text == 'exact' for o in missed)}")

    paired = [o for o in outcomes if o.finding]
    other_type = sum(_SAME_TYPE.get(o.label.type, o.label.type) != o.finding.entity for o in paired)
    lines.append(f"\nFindings on these pages: {findings}; paired with a label: {len(paired)}; "
                 f"unmatched (possible false positives, or PII not labelled): {len(unmatched)}")
    for name, count in sorted(Counter(f"{f.entity}/{f.location}" for f in unmatched).items()):
        lines.append(f"  {name:<22}{count:>4}")
    lines.append(f"Paired, but given a different type from the label: {other_type}")
    return "\n".join(lines).strip("\n")


def _label_row(outcome: Outcome) -> dict:
    label, f = outcome.label, outcome.finding
    return {
        "sheet_row": label.row, "page": label.page, "section": label.section, "location": label.location,
        "type": label.type, "label_text": label.text, "outcome": outcome.how, "edits": outcome.distance or "",
        "found_text": " ".join(f.text.split()) if f else "", "found_entity": f.entity if f else "",
        "found_location": f.location if f else "", "detector": f.detector if f else "",
        "in_extracted_text": outcome.in_text, "extracted_context": outcome.context, "notes": label.notes,
    }


def _write(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:  # the BOM makes Excel read it as UTF-8
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_report(folder: str | Path, doc: Document, outcomes: list[Outcome], unmatched: list[Finding], text: str) -> list[Path]:
    """Write the summary and the row-level files. These hold values: keep them in outputs/."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rows = [_label_row(o) for o in outcomes]
    columns = list(rows[0]) if rows else ["sheet_row"]
    blocks = {b.block_id: b for b in doc.blocks}
    spare = []
    for f in sorted(unmatched, key=lambda f: (f.page or 0, blocks[f.block_id].order, f.start)):
        around = blocks[f.block_id].text
        context = f"{around[max(0, f.start - _CONTEXT):f.start]}[[{around[f.start:f.end]}]]{around[f.end:f.end + _CONTEXT]}"
        spare.append({
            "page": f.page, "entity": f.entity, "found_text": " ".join(f.text.split()), "location": f.location,
            "detector": f.detector, "rule": f.rule, "confidence": f.confidence, "flags": " ".join(f.flags),
            "extracted_context": " ".join(context.split()),
        })
    files = {
        "labels.csv": rows,
        "misses.csv": [r for r in rows if r["outcome"] == "missed"],
        "near_misses.csv": [r for r in rows if r["outcome"] in ("near", "partial", "garbled")],
    }
    written = []
    for name, selected in files.items():
        _write(folder / name, selected, columns)
        written.append(folder / name)
    _write(folder / "unmatched_findings.csv", spare, list(spare[0]) if spare else ["page"])
    (folder / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return written + [folder / "unmatched_findings.csv", folder / "summary.txt"]


def comparison(runs: dict[str, list[Outcome]]) -> str:
    """Two or more runs over the same labels, side by side. Counts only."""
    names = list(runs)
    lines = [f"  {'':<22}" + "".join(f"{name:>24}" for name in names)]

    def row(title: str, count) -> None:
        cells = []
        for name in names:
            n = len(runs[name])
            cells.append(f"{count(runs[name]):>4} of {n:<4}{_share(count(runs[name]), n):>8}    ")
        lines.append(f"  {title:<22}" + "".join(f"{cell:>24}" for cell in cells))

    row("read by OCR (exact)", lambda outcomes: sum(o.in_text == "exact" for o in outcomes))
    row("read by OCR (+near)", lambda outcomes: sum(o.in_text != "no" for o in outcomes))
    row("detected (exact)", lambda outcomes: sum(o.how == "exact" for o in outcomes))
    row("detected (+near)", lambda outcomes: sum(o.how in ("exact", "near") for o in outcomes))
    row("detected (+partial)", lambda outcomes: sum(o.how in ("exact", "near", "partial") for o in outcomes))
    row("detected (+garbled)", lambda outcomes: sum(o.how != "missed" for o in outcomes))
    kinds = sorted({o.label.type for outcomes in runs.values() for o in outcomes})
    lines.append("")
    lines.append("  detected (+garbled), by type")
    for kind in kinds:
        cells = []
        for name in names:
            mine = [o for o in runs[name] if o.label.type == kind]
            cells.append(f"{sum(o.how != 'missed' for o in mine):>4} of {len(mine):<4}"
                         f"{_share(sum(o.how != 'missed' for o in mine), len(mine)):>8}    ")
        lines.append(f"  {kind:<22}" + "".join(f"{cell:>24}" for cell in cells))
    first, last = runs[names[0]], runs[names[-1]]
    gained = sum(a.how == "missed" and b.how != "missed" for a, b in zip(first, last))
    lost = sum(a.how != "missed" and b.how == "missed" for a, b in zip(first, last))
    lines.append("")
    lines.append(f"  labels found only with {names[-1]}: {gained}; found only with {names[0]}: {lost}")
    return "\n".join(lines)


def _pages(spec: str) -> set[int]:
    pages: set[int] = set()
    for part in spec.split(","):
        low, _, high = part.partition("-")
        pages.update(range(int(low), int(high or low) + 1))
    return pages


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("document", help="the PDF, DOCX or PPTX the labels describe")
    parser.add_argument("--labels", required=True, help="the workbook of labels (.xlsx)")
    parser.add_argument("--sheet", required=True, help="the tab to read")
    parser.add_argument("--pages", help="e.g. 15-21 or 3,7-9; default: the pages the labels cover")
    parser.add_argument("--with", dest="others", nargs="*", default=[], metavar="FILE",
                        help="other files to process alongside, so names found in them are searched for here")
    parser.add_argument("--out", default="outputs/eval")
    parser.add_argument("--no-ner", action="store_true", help="rules only: fast, but finds no names")
    parser.add_argument("--location", help="score only labels with this location, e.g. image")
    parser.add_argument("--compare-ocr", action="store_true",
                        help="run twice: pictures read by Tesseract alone, then by Tesseract and docTR")
    args = parser.parse_args(argv)

    from .. import detect as detection
    from ..extract import extract, images
    from ..pipeline import look_again

    path = Path(args.document)
    kind = path.suffix.lstrip(".").lower()
    labels = [l for l in load(args.labels, args.sheet) if not l.file or l.file.lower() in (kind, path.name.lower())]
    pages = _pages(args.pages) if args.pages else {l.page for l in labels}
    labels = [l for l in labels if l.page in pages and (not args.location or l.location == args.location.lower())]
    print(f"{len(labels)} labels on {len(pages)} pages ({min(pages)} to {max(pages)})"
          + (f", location {args.location}" if args.location else ""))

    def detect(docs):
        return detection.detect(docs, use_ner=not args.no_ner)

    images.use_cache(Path(args.out) / "cache")

    def measure(folder: Path) -> list[Outcome]:
        clock = time.perf_counter()
        docs = [extract(p) for p in [path, *map(Path, args.others)]]
        extracted = time.perf_counter()
        print(f"extraction: {extracted - clock:.0f} s")
        found = look_again(docs, detect(docs), detect)
        print(f"detection:  {time.perf_counter() - extracted:.0f} s")

        doc = docs[0]
        mine = [f for f in found if f.doc_id == doc.doc_id and f.page in pages
                and (not args.location or f.location == args.location.lower())]
        outcomes, unmatched = compare(labels, mine, page_texts(doc))
        text = summary(outcomes, unmatched, len(mine))
        print(text)
        print()
        for written in write_report(folder, doc, outcomes, unmatched, text):
            print("wrote", written)
        print(f"total: {time.perf_counter() - clock:.0f} s")
        return outcomes

    if not args.compare_ocr:
        measure(Path(args.out))
        return
    runs = {}
    before = os.environ.get("CERBERUS_OCR")
    try:
        for name, setting in (("Tesseract alone", "tesseract"), ("Tesseract + docTR", "both")):
            print(f"\n=== pictures read by: {name} ===")
            os.environ["CERBERUS_OCR"] = setting
            runs[name] = measure(Path(args.out) / setting)
    finally:
        os.environ.pop("CERBERUS_OCR", None) if before is None else os.environ.update(CERBERUS_OCR=before)
    text = comparison(runs)
    print("\n=== comparison ===")
    print(text)
    target = Path(args.out) / "ocr_comparison.txt"
    target.write_text(text + "\n", encoding="utf-8")
    print("\nwrote", target)


if __name__ == "__main__":
    main()
