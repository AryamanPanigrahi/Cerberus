"""The whole pipeline, from files to verified outputs, in one call.

    extract -> detect (until nothing new) -> resolve to tokens -> redact -> verify -> release

Outputs are written to a "pending" folder first. Only when the verification
pass finds nothing are they moved to their final place. If it finds anything,
they are moved to "withheld" and the run fails: nothing that failed the check
is left where a later step could pick it up.

An audit report (tokens, counts and locations; no values) is written to
"report" either way, so there is a record of what was found.

Run:  python -m cerberus.pipeline data/case2 --out outputs/run
"""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import detect as detection
from .detect.finding import Finding
from .detect.merge import resolve_overlaps
from .extract import extract
from .extract.ir import Document
from .redact import tokens, verify
from .redact.docx import mask_docx
from .redact.pdf import mask_pdf
from .redact.pptx import mask_pptx
from .redact.text import redact_document, redacted_text
from .report import audit

_MASKERS = {".pdf": mask_pdf, ".docx": mask_docx, ".pptx": mask_pptx}
_ROUNDS = 3  # at most this many extra looks; verification still has the last word


@dataclass
class Result:
    documents: list[Document]
    tokenized: list[tokens.Tokenized]
    redacted: list[Path] = field(default_factory=list)  # tokenised text, for the downstream model
    masked: list[Path] = field(default_factory=list)  # masked copies in the original formats
    problems: list[verify.Problem] = field(default_factory=list)
    reports: list[Path] = field(default_factory=list)  # audit log and summaries: tokens and counts, no values


def run(paths: list[Path], out: str | Path, use_ner: bool = True, review: bool = False) -> Result:
    """Process the files together and release the outputs only if they verify clean.

    Raises verify.VerificationError, after moving the outputs to out/withheld,
    if any PII is found in them.
    """
    out = Path(out)
    pending = out / "pending"
    if pending.exists():
        shutil.rmtree(pending)

    def detect(docs):
        return detection.detect(docs, use_ner=use_ner)

    documents = [extract(path) for path in paths]
    found = detection.detect(documents, use_ner=use_ner, review=out / "review" / "names.txt" if review else None)
    found = look_again(documents, found, detect)
    vault_path = out / "vault.json"
    tokenized, vault = tokens.assign(found, tokens.Vault.load(vault_path))
    vault.save(vault_path)  # holds original values: stays in the output folder, never committed
    result = Result(documents, tokenized)

    def withhold() -> None:
        withheld = out / "withheld"
        if withheld.exists():
            shutil.rmtree(withheld)
        if pending.exists():
            shutil.move(str(pending), str(withheld))

    def report(verified: bool) -> None:
        """Write the audit report, and check it like any other output: it must hold no value."""
        result.reports = audit.write(out / "report", documents, tokenized, verified)
        values = verify.known_values(tokenized)
        leaks = [
            verify.Problem(written.name, "raw", "report", token)
            for written in result.reports
            for token in verify.find_values(written.read_text(encoding="utf-8-sig"), values)
        ]
        if leaks:
            shutil.rmtree(out / "report")
            result.reports = []
            result.problems += leaks

    try:
        for path, doc in zip(paths, documents):
            text = redacted_text(doc, tokenized)
            target = pending / "redacted" / f"{path.stem}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            result.problems += verify.check_text(
                target.name, text, tokenized, detect, blocks=redact_document(doc, tokenized)
            )

            masked = _MASKERS[path.suffix.lower()](path, doc, tokenized, pending / "masked" / path.name)
            result.problems += verify.check_file(masked, tokenized, detect)
    except Exception:
        withhold()  # a writer refused, or something broke: half-finished outputs are not left lying about
        raise

    if result.problems:
        withhold()
        report(False)  # the audit still needs to know what was found, and that nothing was released
        verify.ensure_clean(result.problems)

    for kind, collected in (("redacted", result.redacted), ("masked", result.masked)):
        final = out / kind
        final.mkdir(parents=True, exist_ok=True)
        for item in sorted((pending / kind).iterdir()):
            destination = final / item.name
            if destination.exists():
                destination.unlink()
            shutil.move(str(item), str(destination))
            collected.append(destination)
    shutil.rmtree(pending)
    report(True)
    verify.ensure_clean(result.problems)  # only the report itself can have added one by now
    if (out / "withheld").exists():  # left by an earlier failed run; it no longer describes this folder
        shutil.rmtree(out / "withheld")
    return result


def look_again(documents: list[Document], found: list[Finding], detect, rounds: int = _ROUNDS) -> list[Finding]:
    """Repeat detection on the tokenised text until it finds nothing new.

    A model reads context. Once the names it found are replaced by tokens, the
    text around a name it missed looks different, and it may now find it (or
    take a harmless phrase for a name). So one pass is not a stable answer.
    Each round tokenises the documents in memory, runs the detectors over
    that, and maps anything new back to its place in the original text.

    This runs on the extracted blocks, with no file written or re-read, so a
    round costs one detection and nothing else.
    """
    for _ in range(rounds):
        tokenized, _ = tokens.assign(found)
        replaced: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
        for item in tokenized:
            f = item.finding
            replaced.setdefault((f.doc_id, f.block_id), []).append((f.start, f.end, item.token))
        originals = {(d.doc_id, b.block_id): b for d in documents for b in d.blocks}
        cleaned = [redact_document(doc, tokenized) for doc in documents]
        shown = {(d.doc_id, b.block_id): b for d in cleaned for b in d.blocks}

        extra = []
        for f in detect(cleaned):
            key = (f.doc_id, f.block_id)
            text = shown[key].text
            if any(m.start() < f.end and f.start < m.end() for m in verify.TOKEN.finditer(text)):
                continue  # one of our own tokens
            if verify._NOTICE.fullmatch(text.strip()):
                continue
            # Tokens earlier in the block changed its length: undo that to find the original position.
            shift = 0
            for start, end, token in sorted(replaced.get(key, [])):
                if start + shift < f.start:
                    shift += len(token) - (end - start)
            start, end = f.start - shift, f.end - shift
            if originals[key].text[start:end] != f.text:
                continue  # cannot place it with certainty; verification will still see it
            extra.append(replace(f, start=start, end=end, flags=f.flags + ("second_look",)))
        if not extra:
            break
        found = resolve_overlaps(found + extra)
    return found


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", help="a file, or a folder of PDF / DOCX / PPTX files")
    parser.add_argument("--out", default="outputs/run")
    parser.add_argument("--no-ner", action="store_true", help="rules only: fast, but finds no names")
    parser.add_argument("--review", action="store_true", help="write or apply the name review file")
    args = parser.parse_args(argv)

    source = Path(args.source)
    paths = sorted(p for p in source.iterdir() if p.suffix.lower() in _MASKERS) if source.is_dir() else [source]
    try:
        result = run(paths, args.out, use_ner=not args.no_ner, review=args.review)
    except verify.VerificationError as error:
        raise SystemExit(f"FAILED VERIFICATION\n{error}")
    print(f"{len(result.tokenized)} findings, {len({t.token for t in result.tokenized})} identities")
    for path in result.redacted + result.masked:
        print("released", path)
    for path in result.reports:
        print("report  ", path)
    print("verification: both checks passed on every output")


if __name__ == "__main__":
    main()
