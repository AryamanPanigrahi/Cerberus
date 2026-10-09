"""Reconcile findings that overlap: in the same block, or at the same place in a picture."""

from __future__ import annotations

from dataclasses import replace

from ..extract.ir import BBox, Document
from .finding import Finding


def resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Keep one finding where several cover the same characters.

    The same digits can match more than one pattern (a card number contains
    something shaped like a phone number). The finding we are surest of wins;
    between equals, the longer one. Findings that do not overlap are all kept.
    """
    kept: list[Finding] = []
    ranked = sorted(findings, key=lambda f: (-f.confidence, -(f.end - f.start), f.start, f.entity))
    for finding in ranked:
        clash = any(
            k.doc_id == finding.doc_id
            and k.block_id == finding.block_id
            and k.start < finding.end
            and finding.start < k.end
            for k in kept
        )
        if not clash:
            kept.append(finding)
    return sorted(kept, key=lambda f: (f.doc_id, f.block_id, f.start))


def box_of(doc: Document, finding: Finding) -> BBox | None:
    """The rectangle a finding covers on its page or in its picture."""
    block = doc.get(finding.block_id)
    boxes = block.span_boxes(finding.start, finding.end)
    if not boxes:
        return None
    return BBox(min(b.x0 for b in boxes), min(b.y0 for b in boxes), max(b.x1 for b in boxes),
                max(b.y1 for b in boxes), boxes[0].unit)


def one_per_place(docs: list[Document], findings: list[Finding]) -> list[Finding]:
    """Keep one finding where two OCR engines found the same thing in a picture.

    A picture is read by two engines and each reading is searched, so a value
    both of them read is found twice, at the same spot. Counting it twice would
    double every figure in the report. The finding we are surer of is kept and
    flagged "two_engines": two independent readers saw it.

    Nothing is lost for masking. The two findings need not cover the same
    words (one engine may have read the day of a date as part of it, the other
    not), so the rectangle of the finding that is dropped is added to the
    picture's list of places to cover.
    """
    by_id = {doc.doc_id: doc for doc in docs}

    def engine(finding: Finding) -> str | None:
        return (by_id[finding.doc_id].get(finding.block_id).native_ref or {}).get("engine")

    def region(finding: Finding) -> str | None:
        return by_id[finding.doc_id].get(finding.block_id).parent_id

    def same_place(a: BBox, b: BBox) -> bool:
        across, down = min(a.x1, b.x1) - max(a.x0, b.x0), min(a.y1, b.y1) - max(a.y0, b.y0)
        if across <= 0 or down <= 0:
            return False
        return across * down > 0.5 * min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0))

    kept: list[Finding] = []
    seen: dict[tuple, list[tuple[int, str, BBox]]] = {}  # (doc, picture, entity) -> kept findings there
    candidates = [f for f in findings if f.location == "image" and f.doc_id in by_id and engine(f)]
    others = [f for f in findings if not (f.location == "image" and f.doc_id in by_id and engine(f))]
    # docTR reads small print better, so between equals its wording is the one kept.
    for finding in sorted(candidates, key=lambda f: (-f.confidence, engine(f) != "doctr", -(f.end - f.start))):
        box = box_of(by_id[finding.doc_id], finding)
        key = (finding.doc_id, region(finding), finding.entity)
        if box is not None:
            twin = next((i for i, other, place in seen.get(key, []) if other != engine(finding) and same_place(box, place)), None)
            if twin is not None:
                if "two_engines" not in kept[twin].flags:
                    kept[twin] = replace(kept[twin], flags=kept[twin].flags + ("two_engines",))
                picture = by_id[finding.doc_id].get(region(finding))
                entry = ["finding", box.x0, box.y0, box.x1, box.y1]
                cover = (picture.native_ref or {}).setdefault("cover", []) if picture.native_ref is not None else None
                if cover is not None and entry not in cover:
                    cover.append(entry)
                continue
            seen.setdefault(key, []).append((len(kept), engine(finding), box))
        kept.append(finding)
    return sorted(others + kept, key=lambda f: (f.doc_id, f.block_id, f.start))
