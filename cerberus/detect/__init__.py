"""Detection: every detector runs on the original text, then the results are reconciled."""

from __future__ import annotations

from pathlib import Path

from ..extract.ir import Document
from .finding import Finding
from .merge import one_per_place, resolve_overlaps


def detect(docs: list[Document], use_ner: bool = True, review: str | Path | None = None) -> list[Finding]:
    """Run the detectors over a set of documents and return one list of findings.

    Each detector sees the untouched text. Nothing is redacted between them:
    removing an e-mail address first would take away the words a name detector
    relies on. Overlaps are settled once, at the end, and a value two OCR
    engines both read in a picture is reported once.

    review is an optional file for a person to check the list of names. If it
    does not exist it is written; if it does, its decisions are applied. The
    pipeline gives the same result without it.
    """
    from . import rules  # imported here so that using one detector does not load the others

    findings = rules.detect(docs)
    if use_ner:
        from . import dictionary, ner

        people = ner.detect(docs)
        keep = drop = frozenset()
        if review is not None and Path(review).exists():
            keep, drop = dictionary.read_review(review)
        elif review is not None:
            dictionary.write_review(review, dictionary.candidates(docs, findings + people))
        # A name a person has ruled out is removed everywhere, not just from the sweep.
        people = [f for f in people if not (f.entity == "PERSON" and dictionary._key(f.text) in drop)]
        findings += people
        # Names with independent evidence are now searched for everywhere.
        findings += dictionary.detect(docs, findings, keep, drop)
    return one_per_place(docs, resolve_overlaps(findings))
