"""Precision and recall of PII detection, against a person's judgement.

Detection can only be scored against an answer a person has written down.
Typing every PII instance in three documents from scratch is slow, so the
work is turned round: the pipeline writes a sheet of what it found, and a
person marks each row and adds what is missing.

One CSV per document (opens in Excel), one row per finding:

    verdict   ok      the row is PII of the stated kind
              wrong   it is not PII, or not that kind
              missed  a row the person ADDS for PII the pipeline did not find
              (blank) not checked yet; left out of the score and counted

    For an added "missed" row fill in: entity, value, page, location.

From the verdicts:

    precision = ok / (ok + wrong)      of what we reported, how much was right
    recall    = ok / (ok + missed)     of what was there, how much we reported

Recall is only as good as the search for misses: the person has to read the
document, not just the sheet.

Re-exporting after the pipeline changes keeps the verdicts already given, and
a "missed" row the pipeline now finds becomes an "ok" row by itself.

Run:  python -m cerberus.eval.pii export data/case2 --out labels/pii
      python -m cerberus.eval.pii score labels/pii
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..extract.ir import Document
from ..redact.tokens import Tokenized

COLUMNS = ["verdict", "entity", "value", "page", "location", "token", "context", "detector", "block", "start", "end"]
_VERDICTS = {"ok", "wrong", "missed"}


def _norm(value: str) -> str:
    return " ".join(str(value).lower().split())


def _key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("page") or ""), row["entity"], _norm(row["value"]))


def rows_for(doc: Document, tokenized: list[Tokenized]) -> list[dict]:
    """One sheet row per finding in this document, in reading order."""
    blocks = {b.block_id: b for b in doc.blocks}
    rows = []
    mine = [t for t in tokenized if t.finding.doc_id == doc.doc_id]
    for item in sorted(mine, key=lambda t: (blocks[t.finding.block_id].order, t.finding.start)):
        f = item.finding
        text = blocks[f.block_id].text
        context = f"{text[max(0, f.start - 40):f.start]}[[{text[f.start:f.end]}]]{text[f.end:f.end + 40]}"
        rows.append({
            "verdict": "", "entity": f.entity, "value": " ".join(f.text.split()),
            "page": f.page or "", "location": f.location, "token": item.token,
            "context": " ".join(context.split()), "detector": f.detector,
            "block": f.block_id, "start": f.start, "end": f.end,
        })
    return rows


def merge(new: list[dict], old: list[dict]) -> list[dict]:
    """Carry a person's verdicts over to a fresh export.

    Rows are matched by page, entity and value, in order, so they survive a
    change in block numbering. A "missed" row the pipeline now finds turns
    into an "ok" row; one it still misses is kept.
    """
    given: dict[tuple, list[str]] = {}
    missed: dict[tuple, int] = Counter()
    for row in old:
        verdict = row.get("verdict", "").strip().lower()
        if verdict == "missed":
            missed[_key(row)] += 1
        elif verdict in _VERDICTS:
            given.setdefault(_key(row), []).append(verdict)
    for row in new:
        key = _key(row)
        if given.get(key):
            row["verdict"] = given[key].pop(0)
        elif missed[key]:
            missed[key] -= 1
            row["verdict"] = "ok"
    still_missed = []
    for row in old:
        if row.get("verdict", "").strip().lower() == "missed" and missed[_key(row)] > 0:
            missed[_key(row)] -= 1
            still_missed.append({column: row.get(column, "") for column in COLUMNS})
    return new + still_missed


def read(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write(path: str | Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:  # the BOM makes Excel read it as UTF-8
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export(docs: list[Document], tokenized: list[Tokenized], folder: str | Path) -> list[Path]:
    written = []
    for doc in docs:
        path = Path(folder) / f"{Path(doc.name).stem}.csv"
        rows = rows_for(doc, tokenized)
        write(path, merge(rows, read(path)) if path.exists() else rows)
        written.append(path)
    return written


@dataclass
class Tally:
    ok: int = 0
    wrong: int = 0
    missed: int = 0
    unchecked: int = 0

    @property
    def precision(self) -> float | None:
        return self.ok / (self.ok + self.wrong) if self.ok + self.wrong else None

    @property
    def recall(self) -> float | None:
        return self.ok / (self.ok + self.missed) if self.ok + self.missed else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else None

    def add(self, verdict: str) -> None:
        name = verdict if verdict in _VERDICTS else "unchecked"
        setattr(self, name, getattr(self, name) + 1)


def score(rows: list[dict]) -> dict[str, dict[str, Tally]]:
    """Tallies overall and split by entity and by location."""
    result: dict[str, dict[str, Tally]] = {"all": {"all": Tally()}, "entity": {}, "location": {}}
    for row in rows:
        verdict = row.get("verdict", "").strip().lower()
        result["all"]["all"].add(verdict)
        result["entity"].setdefault(row["entity"], Tally()).add(verdict)
        result["location"].setdefault(row.get("location") or "?", Tally()).add(verdict)
    return result


def _line(name: str, tally: Tally) -> str:
    def pct(value):
        return f"{value:7.1%}" if value is not None else "      -"

    return (f"  {name:<12} ok={tally.ok:>4} wrong={tally.wrong:>3} missed={tally.missed:>3} "
            f"unchecked={tally.unchecked:>4}  precision={pct(tally.precision)} recall={pct(tally.recall)} "
            f"F1={pct(tally.f1)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    exporting = commands.add_parser("export", help="write a sheet per document for a person to mark")
    exporting.add_argument("source", help="a folder of PDF / DOCX / PPTX files")
    exporting.add_argument("--out", default="labels/pii")
    scoring = commands.add_parser("score", help="score the marked sheets")
    scoring.add_argument("folder")
    args = parser.parse_args(argv)

    if args.command == "export":
        from .. import detect as detection
        from ..extract import extract
        from ..redact import tokens

        paths = sorted(p for p in Path(args.source).iterdir() if p.suffix.lower() in (".pdf", ".docx", ".pptx"))
        docs = [extract(path) for path in paths]
        tokenized, _ = tokens.assign(detection.detect(docs))
        for written in export(docs, tokenized, args.out):
            print("wrote", written)
        return

    everything: list[dict] = []
    for path in sorted(Path(args.folder).glob("*.csv")):
        rows = read(path)
        everything += rows
        print(path.name)
        print(_line("all", score(rows)["all"]["all"]))
    total = score(everything)
    print("ALL FILES")
    print(_line("all", total["all"]["all"]))
    for group in ("entity", "location"):
        print(f" by {group}")
        for name, tally in sorted(total[group].items()):
            print(_line(name, tally))
    unchecked = total["all"]["all"].unchecked
    if unchecked:
        print(f"{unchecked} rows have no verdict yet and are left out of precision and recall.")


if __name__ == "__main__":
    main()
