"""Local HTML preview of what the pipeline extracted and detected.

One self-contained page per file, written to a folder under outputs/ (which is
gitignored, because the pages show real document content). Each page has:

- a summary: pages, how many needed OCR, confidence, what was quarantined;
- the findings, counted by entity and location, and listed one by one;
- the document as extracted, in reading order, with every finding highlighted.

This is a development and demonstration aid, not the audit report. It shows
the original values so a person can judge what was found and what was missed.

Run:  python -m cerberus.report.preview data/case2 --out outputs/preview
"""

from __future__ import annotations

import argparse
from collections import Counter
from html import escape
from pathlib import Path

from ..detect import rules
from ..detect.finding import Finding
from ..extract import extract
from ..extract.ir import Block, BlockType, Document

_SUPPORTED = {".pdf", ".docx", ".pptx"}
_COLOURS = {
    "EMAIL": "#cfe8ff", "PHONE": "#d9f2d9", "STAFF_ID": "#ffe4b3", "SSN": "#ffc9c9",
    "TAX_ID": "#ffc9c9", "CARD": "#ffc9c9", "PAN": "#f3d1ff", "PESEL": "#f3d1ff",
    "PASSPORT": "#f3d1ff", "DOB": "#fff3a8",
}
_STYLE = """
body{font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;color:#1c2330;background:#f5f6f8}
main{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid #cfd4dc;padding-bottom:4px}
.note{color:#5b6472;font-size:13px}
table{border-collapse:collapse;background:#fff;font-size:13px;margin:6px 0 14px}
td,th{border:1px solid #d5d9e0;padding:3px 8px;text-align:left;vertical-align:top}
th{background:#e9ecf1} .num{text-align:right}
.doc{background:#fff;border:1px solid #d5d9e0;padding:18px 26px}
.doc h3{margin:18px 0 4px;font-size:16px} .doc h4{margin:14px 0 4px;font-size:15px} .doc h5{margin:12px 0 4px;font-size:14px}
.doc p{margin:5px 0} .doc li{margin:2px 0 2px 18px}
.page{margin:22px -26px 10px;padding:3px 26px;background:#e9ecf1;color:#5b6472;font-size:12px;letter-spacing:.04em}
.box{border:1px dashed #9aa3b2;border-radius:6px;padding:6px 10px;margin:8px 0;background:#fafbfc}
.box>.tag{font-size:11px;color:#5b6472;text-transform:uppercase;letter-spacing:.05em}
.box.q{border-color:#c0392b;background:#fdf1ef}
.furniture{color:#8a93a1;font-size:12px}
mark{padding:0 2px;border-radius:3px} mark sub{font-size:9px;color:#444;margin-left:2px}
.chips span{display:inline-block;padding:1px 8px;border-radius:10px;margin:0 6px 4px 0;font-size:12px}
"""


def build(paths: list[Path], out: Path) -> list[Path]:
    """Extract and scan the files together, then write one page each plus an index."""
    docs = [extract(path) for path in paths]
    findings = rules.detect(docs)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for doc in docs:
        mine = [f for f in findings if f.doc_id == doc.doc_id]
        target = out / f"{Path(doc.name).stem}.html"
        target.write_text(_page(doc, mine), encoding="utf-8")
        written.append(target)
    rows = "".join(
        f"<tr><td><a href='{escape(p.name)}'>{escape(d.name)}</a></td><td class=num>{len(d.blocks)}</td>"
        f"<td class=num>{sum(f.doc_id == d.doc_id for f in findings)}</td></tr>"
        for d, p in zip(docs, written)
    )
    index = out / "index.html"
    index.write_text(
        f"<!doctype html><meta charset=utf-8><title>Cerberus preview</title><style>{_STYLE}</style><main>"
        f"<h1>Cerberus preview</h1><p class=note>Local only. These pages show original document content.</p>"
        f"<table><tr><th>File</th><th>Blocks</th><th>Findings</th></tr>{rows}</table></main>",
        encoding="utf-8",
    )
    return [index, *written]


def _page(doc: Document, findings: list[Finding]) -> str:
    by_block: dict[str, list[Finding]] = {}
    for finding in findings:
        by_block.setdefault(finding.block_id, []).append(finding)
    children: dict[str | None, list[Block]] = {}
    for block in doc.blocks:
        children.setdefault(block.parent_id, []).append(block)

    parts = [
        f"<!doctype html><meta charset=utf-8><title>{escape(doc.name)}</title><style>{_STYLE}</style><main>",
        f"<h1>{escape(doc.name)}</h1><p class=note><a href='index.html'>all files</a> · local only: "
        "this page shows original document content</p>",
        _summary(doc, findings),
        _finding_tables(findings),
        "<h2>Document as extracted</h2><p class=note>Reading order. Dashed boxes are groups of shapes "
        "or pictures; red boxes are quarantined (read too poorly to trust).</p><div class=doc>",
    ]
    state = {"page": None}
    for block in children.get(None, []):
        parts.append(_render(block, children, by_block, state))
    parts.append("</div></main>")
    return "".join(parts)


def _summary(doc: Document, findings: list[Finding]) -> str:
    kinds = Counter(b.type.value for b in doc.blocks)
    rows = [("Format", doc.format), ("Blocks", str(len(doc.blocks)))]
    if doc.pages:
        confidences = [p.ocr_conf for p in doc.pages if p.ocr_conf is not None]
        rows += [
            ("Pages or slides", str(len(doc.pages))),
            ("Needed OCR", f"{len(doc.ocr_pages())} of {len(doc.pages)}"),
            ("Quarantined pages", str(sum(p.quarantined for p in doc.pages))),
        ]
        if confidences:
            rows.append(("Page OCR confidence", f"{min(confidences):.2f} to {max(confidences):.2f}"))
    pictures = [b for b in doc.blocks if b.type is BlockType.IMAGE_REGION]
    if pictures:
        rows.append(("Pictures", f"{len(pictures)}, of which {sum(b.quarantined for b in pictures)} quarantined"))
    rows.append(("Structure", ", ".join(f"{count} {kind}" for kind, count in kinds.most_common())))
    rows.append(("Findings (rule detector only; names not yet detected)", str(len(findings))))
    body = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in rows)
    return f"<h2>Summary</h2><table>{body}</table>"


def _finding_tables(findings: list[Finding]) -> str:
    if not findings:
        return "<h2>Findings</h2><p class=note>None from the rule detector.</p>"
    locations = ("body", "table", "prose", "image")
    counts: dict[str, Counter] = {}
    for f in findings:
        counts.setdefault(f.entity, Counter())[f.location] += 1
    head = "".join(f"<th>{name}</th>" for name in locations)
    rows = "".join(
        f"<tr><td><mark style='background:{_COLOURS.get(entity, '#eee')}'>{entity}</mark></td>"
        f"<td class=num>{sum(c.values())}</td>"
        + "".join(f"<td class=num>{c[name] or ''}</td>" for name in locations)
        + "</tr>"
        for entity, c in sorted(counts.items(), key=lambda item: -sum(item[1].values()))
    )
    listing = "".join(
        f"<tr><td class=num>{f.page or ''}</td><td>{f.location}</td><td>{f.entity}</td>"
        f"<td>{escape(f.subtype)}</td><td>{escape(f.text)}</td><td>{f.rule}</td>"
        f"<td class=num>{f.confidence:.2f}</td><td>{escape(', '.join(f.flags))}</td></tr>"
        for f in findings
    )
    return (
        f"<h2>Findings by entity and location</h2><table><tr><th>Entity</th><th>Total</th>{head}</tr>{rows}</table>"
        f"<details><summary>All {len(findings)} findings, one per row</summary><table><tr><th>Page</th>"
        f"<th>Location</th><th>Entity</th><th>Subtype</th><th>Value</th><th>Rule</th><th>Confidence</th>"
        f"<th>Flags</th></tr>{listing}</table></details>"
    )


def _mark(block: Block, by_block: dict[str, list[Finding]]) -> str:
    """The block's text with each finding wrapped in a coloured mark."""
    out, position = [], 0
    for f in sorted(by_block.get(block.block_id, []), key=lambda f: f.start):
        out.append(escape(block.text[position : f.start]))
        title = escape(f"{f.rule}, confidence {f.confidence:.2f}" + (f", {', '.join(f.flags)}" if f.flags else ""))
        out.append(
            f"<mark style='background:{_COLOURS.get(f.entity, '#eee')}' title='{title}'>"
            f"{escape(block.text[f.start : f.end])}<sub>{f.entity}</sub></mark>"
        )
        position = f.end
    out.append(escape(block.text[position:]))
    return "".join(out).replace("\n", "<br>")


def _render(block: Block, children, by_block, state) -> str:
    out = []
    if block.page is not None and block.page != state["page"]:
        state["page"] = block.page
        out.append(f"<div class=page>PAGE / SLIDE {block.page}</div>")
    kids = children.get(block.block_id, [])
    inner = lambda: "".join(_render(kid, children, by_block, state) for kid in kids)  # noqa: E731
    text = _mark(block, by_block)
    quarantined = " q" if block.quarantined else ""

    if block.type is BlockType.HEADING:
        level = min(5, 3 + len(block.section_path))
        out.append(f"<h{level}>{text}</h{level}>{inner()}")
    elif block.type is BlockType.TABLE:
        out.append(_table(kids, children, by_block, state))
    elif block.type in (BlockType.GROUP, BlockType.IMAGE_REGION):
        name = "picture" if block.type is BlockType.IMAGE_REGION else "group"
        detail = ""
        if block.type is BlockType.IMAGE_REGION:
            detail = f" · confidence {block.ocr_conf:.2f}" if block.ocr_conf is not None else " · no text read"
            detail += " · quarantined" if block.quarantined else ""
        out.append(f"<div class='box{quarantined}'><div class=tag>{name}{detail}</div>{inner()}</div>")
    elif block.type is BlockType.SLIDE:
        out.append(inner())
    elif block.type in (BlockType.HEADER, BlockType.FOOTER, BlockType.NOTES):
        label = block.type.value
        out.append(f"<div class=furniture>[{label}] {text}{inner()}</div>")
    elif block.type is BlockType.METADATA:
        ref = block.native_ref or {}
        label = next((f"{k}: {v}" for k, v in ref.items() if k not in ("part", "p", "shape_id") and v is not True), "")
        label = label or next((k for k, v in ref.items() if v is True), "property")
        out.append(f"<div class=furniture>[hidden · {escape(str(label))}] {text}</div>")
    elif block.type is BlockType.LIST_ITEM:
        out.append(f"<li>{text}</li>{inner()}")
    else:
        out.append(f"<p>{text}</p>{inner()}")
    return "".join(out)


def _table(cells: list[Block], children, by_block, state) -> str:
    rows: dict[int, list[Block]] = {}
    for cell in cells:
        if cell.type is BlockType.TABLE_CELL:
            rows.setdefault(cell.row, []).append(cell)
    has_header = any(cell.header_id for cell in cells)
    html = ["<table>"]
    for number in sorted(rows):
        tag = "th" if number == 0 and has_header else "td"
        html.append("<tr>")
        for cell in sorted(rows[number], key=lambda c: c.col):
            nested = "".join(_render(kid, children, by_block, state) for kid in children.get(cell.block_id, []))
            html.append(f"<{tag}>{_mark(cell, by_block)}{nested}</{tag}>")
        html.append("</tr>")
    html.append("</table>")
    return "".join(html)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", help="a file, or a folder of PDF / DOCX / PPTX files")
    parser.add_argument("--out", default="outputs/preview")
    args = parser.parse_args(argv)
    source = Path(args.source)
    paths = sorted(p for p in source.iterdir() if p.suffix.lower() in _SUPPORTED) if source.is_dir() else [source]
    for written in build(paths, Path(args.out)):
        print("wrote", written)


if __name__ == "__main__":
    main()
