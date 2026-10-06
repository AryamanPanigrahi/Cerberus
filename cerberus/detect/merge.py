"""Reconcile findings that overlap in the same block."""

from __future__ import annotations

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
