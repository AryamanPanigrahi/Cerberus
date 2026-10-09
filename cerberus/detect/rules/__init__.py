"""Rule detector: patterns, validators and context keywords.

For identifiers with a fixed shape this is the most precise detector we have,
and the only one whose every decision can be explained in one sentence. Each
rule combines up to three kinds of evidence:

- shape: a regular expression;
- structure: a validator (checksum, embedded date, holder type);
- context: a keyword nearby, in the block's own text or in the label of its
  table cell ("SSN", "date of birth", "passport").

Confidence rises with the evidence. Shapes that mean nothing on their own (a
date, four digits, a short code) are reported only with context.

Names and addresses have no fixed shape; they are left to the NER detector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

from ...extract.ir import Block, BlockType, Container, Document, Source
from .. import allowlist
from ..finding import Finding, location_of, make
from ..merge import resolve_overlaps
from . import validators


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    entity: str
    rule: str
    confidence: float
    subtype: str = ""
    flags: tuple[str, ...] = ()
    labelled: bool = False  # a context keyword supported it; reported as the "keyword" flag


class _Context:
    """What a rule may look at besides the match itself."""

    def __init__(
        self, text: str, label: str, ocr: bool, staff_prefixes: frozenset[str],
        picture: bool = False, domains: frozenset[str] = frozenset(), common: frozenset[str] = frozenset(),
    ):
        self.text = text
        self.lower = text.lower()
        self.label = label.lower()  # the cell's column header and row label, or the line above in a picture
        self.ocr = ocr
        self.staff_prefixes = staff_prefixes
        self.picture = picture  # the text was read from inside a picture
        self.domains = domains  # e-mail domains seen written correctly somewhere in these documents
        self.common = common  # words this document writes in small letters: ordinary words, not names

    def near(self, start: int, end: int, words: tuple[str, ...], before: int = 60, after: int = 12) -> bool:
        """Is one of these words in the label, or just before / after the match?"""
        window = self.lower[max(0, start - before) : start] + " " + self.lower[end : end + after]
        return any(_has_word(self.label, w) or _has_word(window, w) for w in words)


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) is not None


# --- e-mail ---------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# OCR reads a leading "l" as "|", and may put a space before the "@" or before the last part.
_EMAIL_OCR = re.compile(
    r"[A-Za-z0-9|][A-Za-z0-9._%+|-]*\s?@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?:\.\s?[a-z]{2,10})(?![A-Za-z0-9-])"
)


def _emails(ctx: _Context) -> Iterator[Hit]:
    for m in (_EMAIL_OCR if ctx.ocr else _EMAIL).finditer(ctx.text):
        end = m.end() - len(m.group()) + len(m.group().rstrip(".-"))
        yield Hit(m.start(), end, "EMAIL", "email", 0.95, labelled=ctx.near(m.start(), end, ("email", "e-mail", "mail")))
    if ctx.ocr and ctx.domains:
        yield from _emails_without_at(ctx)


_WORD = re.compile(r"\S+")
_AT_LOOKALIKES = "eaocgq0©&€"  # what OCR makes of an "@" in small print


def _emails_without_at(ctx: _Context) -> Iterator[Hit]:
    """An address whose "@" the OCR read as a letter: "j.sampleecorp.example".

    Without the "@" nothing marks it as an address, except that it ends in a
    domain these documents use. So the end of each word is compared with the
    domains seen written correctly elsewhere, allowing two wrong characters,
    and the character before it must be one an "@" is commonly read as.
    """
    for m in _WORD.finditer(ctx.text):
        word = m.group().rstrip(".,;:)")
        if "@" in word or "." not in word:
            continue
        lower = word.lower()
        best = None
        for domain in ctx.domains:
            for cut in range(max(1, len(lower) - len(domain) - 2), len(lower) - len(domain) + 3):
                wrong = _edits(lower[cut:], domain, 2)
                if wrong <= 2 and (best is None or wrong < best[0]):
                    best = (wrong, cut)
        # Where the domain fits best, the character in front of it must be a misread "@":
        # "www.corp.example" fits best after a full stop, and is a web address.
        if best is None or best[1] < 4 or lower[best[1] - 1] not in _AT_LOOKALIKES:
            continue
        local = word[: best[1] - 1]
        # A log line glues a key in front ("user=j.sample..."); the address starts after it.
        key = max(local.rfind("="), local.rfind(":"))
        if key < 0 and "-" in local and "." in local.rsplit("-", 1)[1]:
            key = local.rfind("-")
        if len(local) - key - 1 < 3:
            continue
        yield Hit(m.start() + key + 1, m.start() + len(word), "EMAIL", "email_at_misread", 0.7, flags=("ocr_at",))


def _edits(a: str, b: str, limit: int) -> int:
    """Characters to insert, delete or replace to turn a into b; limit + 1 if more."""
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
    return previous[-1]


def learn_domains(docs: list[Document]) -> frozenset[str]:
    """E-mail domains written correctly at least twice in these documents."""
    seen: dict[str, int] = {}
    for doc in docs:
        for block in doc.blocks:
            for m in _EMAIL.finditer(block.text):
                domain = m.group().rsplit("@", 1)[1].lower().rstrip(".")
                seen[domain] = seen.get(domain, 0) + 1
    return frozenset(domain for domain, count in seen.items() if count >= 2)


# --- phone ----------------------------------------------------------------

_PHONE_INTL = re.compile(
    r"(?<![\w+])\+\d{1,3}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?\d{1,4}(?:[\s.-]?\d{2,4}){1,4}(?!\w)"
)
_PHONE_US = re.compile(r"(?<!\w)(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}(?!\w)")
_PHONE_WORDS = ("phone", "mobile", "tel", "telephone", "cell", "contact", "line", "fax", "call")


def _phones(ctx: _Context) -> Iterator[Hit]:
    for pattern, rule, base in ((_PHONE_INTL, "phone_international", 0.85), (_PHONE_US, "phone_us", 0.75)):
        for m in pattern.finditer(ctx.text):
            digits = re.sub(r"\D", "", m.group())
            if not 8 <= len(digits) <= 15:
                continue
            labelled = ctx.near(m.start(), m.end(), _PHONE_WORDS)
            yield Hit(m.start(), m.end(), "PHONE", rule, min(0.95, base + 0.1 * labelled), labelled=labelled)


# --- US social security number and tax identifiers --------------------------

_SSN = re.compile(r"(?<![\w-])(\d{3})[-. ](\d{2})[-. ](\d{4})(?![\w-])")
_SSN_STRICT = re.compile(r"(?<![\w-])(\d{3})-(\d{2})-(\d{4})(?![\w-])")
_SSN_WORDS = ("ssn", "social security", "social security number")
_TAX_WORDS = ("tin", "itin", "ein", "tax id", "tax identifier", "tax identification", "taxpayer", "tax number", "nip")
_LAST_FOUR = re.compile(
    r"(?:last\s+(?:four|4)(?:\s+digits)?|ending(?:\s+in)?|ends\s+in)\D{0,40}?(?<!\d)(\d{4})(?!\d)", re.I
)
_EIN = re.compile(r"(?<![\w-])\d{2}-\d{7}(?![\w-])")
_TAX_PLAIN = re.compile(r"(?<![\w-])\d{9,11}(?![\w-])")


def _ssn_and_tax(ctx: _Context) -> Iterator[Hit]:
    # OCR turns a hyphen into a dot or a space; typed text is held to the strict shape.
    for m in (_SSN if ctx.ocr else _SSN_STRICT).finditer(ctx.text):
        flags = validators.ssn_flags(m.group(1), m.group(2), m.group(3))
        if flags is None:
            continue
        tax = ctx.near(m.start(), m.end(), _TAX_WORDS)
        ssn = ctx.near(m.start(), m.end(), _SSN_WORDS)
        entity = "TAX_ID" if tax and not ssn else "SSN"
        yield Hit(m.start(), m.end(), entity, "ssn_shape", 0.9 if (ssn or tax) else 0.7, flags=flags, labelled=ssn or tax)

    for m in _EIN.finditer(ctx.text):
        if ctx.near(m.start(), m.end(), _TAX_WORDS):
            yield Hit(m.start(), m.end(), "TAX_ID", "ein_with_keyword", 0.85, labelled=True)
    for m in _TAX_PLAIN.finditer(ctx.text):
        if ctx.near(m.start(), m.end(), _TAX_WORDS, before=30):
            yield Hit(m.start(), m.end(), "TAX_ID", "tax_number_with_keyword", 0.75, labelled=True)

    # "the last four digits of your SSN (1234)": four digits mean nothing without the words.
    for m in _LAST_FOUR.finditer(ctx.text):
        phrase = ctx.lower[m.start() : m.end()]
        around = ctx.lower[max(0, m.start() - 30) : m.start()] + phrase
        if any(_has_word(around, w) for w in _SSN_WORDS):
            yield Hit(m.start(1), m.end(1), "SSN", "ssn_last_four", 0.8, subtype="partial", labelled=True)
        elif any(_has_word(around, w) for w in _CARD_WORDS):
            yield Hit(m.start(1), m.end(1), "CARD", "card_last_four", 0.8, subtype="partial", labelled=True)


# --- payment card -----------------------------------------------------------

_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")
_CARD_WORDS = ("card", "visa", "mastercard", "amex", "credit card", "debit card", "pan number", "card number")


def _cards(ctx: _Context) -> Iterator[Hit]:
    for m in _CARD.finditer(ctx.text):
        digits = re.sub(r"\D", "", m.group())
        if not 13 <= len(digits) <= 19 or not validators.luhn(digits):
            continue
        # Luhn passes one random number in ten, so a second piece of evidence is
        # required: a card keyword, or the standard grouping with an issuer digit.
        labelled = ctx.near(m.start(), m.end(), _CARD_WORDS)
        grouped = re.fullmatch(r"\d{4}([ -])\d{4}\1\d{4}\1\d{1,4}", m.group()) and digits[0] in "23456"
        if labelled or grouped:
            yield Hit(m.start(), m.end(), "CARD", "card_luhn", 0.95 if labelled else 0.85, labelled=labelled)
    # A number already masked in the source ("4111-XXXX-XXXX-1111") cannot be checked, but its
    # first and last four digits are still personal data: reported as a partial, like a masked PESEL.
    for m in _CARD_MASKED.finditer(ctx.text):
        labelled = ctx.near(m.start(), m.end(), _CARD_WORDS)
        yield Hit(m.start(), m.end(), "CARD", "card_already_masked", 0.85 if labelled else 0.75, subtype="masked", labelled=labelled)


_CARD_MASKED = re.compile(r"(?<!\w)\d{4}[ -]?[xX*•]{4}[ -]?[xX*•]{4}[ -]?\d{4}(?![\w-])")


# --- PAN (India) -------------------------------------------------------------

_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")


def _pans(ctx: _Context) -> Iterator[Hit]:
    for m in _PAN.finditer(ctx.text):
        holder = validators.pan_holder(m.group())
        if holder is None:
            continue
        labelled = ctx.near(m.start(), m.end(), ("pan", "permanent account", "national id", "tax"))
        yield Hit(m.start(), m.end(), "PAN", "pan_structure", 0.95 if labelled else 0.8, subtype=holder, labelled=labelled)
    if ctx.ocr:  # OCR can run a label into its value: "PANABCPS1234D"
        for m in _PAN_GLUED.finditer(ctx.text):
            holder = validators.pan_holder(m.group(1))
            if holder is not None:
                yield Hit(m.start(1), m.end(1), "PAN", "pan_structure", 0.9, subtype=holder, labelled=True)


_PAN_GLUED = re.compile(r"(?<![A-Za-z0-9])PAN[:#.]?([A-Z]{5}\d{4}[A-Z])(?![A-Za-z0-9])")


# --- PESEL (Poland) ----------------------------------------------------------

_PESEL = re.compile(r"(?<![\w-])\d{11}(?![\w-])")
_PESEL_MASKED = re.compile(r"(?<![\w-])(\d{6})[xX*•]{5}(?![\w-])")
_PESEL_WORDS = ("pesel", "national id")


def _pesels(ctx: _Context) -> Iterator[Hit]:
    for m in _PESEL.finditer(ctx.text):
        if validators.pesel(m.group()):
            labelled = ctx.near(m.start(), m.end(), _PESEL_WORDS)
            yield Hit(m.start(), m.end(), "PESEL", "pesel_checksum", 0.95 if labelled else 0.85, labelled=labelled)
    # Already half-masked in the source: the visible six digits are the birth
    # date, which is personal data, so it is still reported and still masked.
    for m in _PESEL_MASKED.finditer(ctx.text):
        if validators.pesel_birth_date(m.group(1)) is None:
            continue
        labelled = ctx.near(m.start(), m.end(), _PESEL_WORDS)
        yield Hit(m.start(), m.end(), "PESEL", "pesel_masked", 0.9 if labelled else 0.6, subtype="masked", labelled=labelled)


# --- passport ----------------------------------------------------------------

_PASSPORT = re.compile(r"passport(?:\s+(?:no|number|num|#))?\.?\s*[:#]?\s*([A-Z0-9]{6,9})(?![A-Za-z0-9])", re.I)


def _passports(ctx: _Context) -> Iterator[Hit]:
    for m in _PASSPORT.finditer(ctx.text):
        value = m.group(1)
        if any(c.isdigit() for c in value) and value.upper() == value:
            yield Hit(m.start(1), m.end(1), "PASSPORT", "passport_with_keyword", 0.85, labelled=True)


# --- date of birth -----------------------------------------------------------

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE = re.compile(
    rf"(?<!\d)(?:\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\s+{_MONTH}\.?\s+\d{{4}}|{_MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}})(?!\d)",
    re.I,
)
_DOB_WORDS = ("dob", "d.o.b", "date of birth", "birth date", "born", "birthday", "birth")


def _births(ctx: _Context) -> Iterator[Hit]:
    # A date is personal data only when something says it is a date of birth.
    for m in _DATE.finditer(ctx.text):
        if ctx.near(m.start(), m.end(), _DOB_WORDS, before=30, after=0):
            yield Hit(m.start(), m.end(), "DOB", "date_with_birth_keyword", 0.85, labelled=True)


# --- staff identifiers -------------------------------------------------------

_CODE = re.compile(r"(?<![A-Za-z0-9-])([A-Z]{2,5})(?:-[A-Z]{2})?-\d{3,6}(?![A-Za-z0-9-])")
# OCR may read the hyphen as a dot, or put a space after it, or read the "=" in front of a
# value as a hyphen ("owner -EMP.12345").
_CODE_OCR = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5})(?:-[A-Z]{2})?[-.]\s?\d{3,6}(?![A-Za-z0-9-])")
_STAFF_LABEL = re.compile(
    r"\b(?:emp(?:loyee)?|staff|personnel|worker|vendor|contractor|member|director|badge|user)\b.{0,12}\bid\b"
    r"|\bemployee\s+identifier\b",
    re.I,
)
_AFTER_NAME = re.compile(r"[A-Z][a-z]+(?:[ -][A-Z][\w.'-]*)+\s*\(\s*$")
_SEED_PREFIXES = frozenset({"EMP", "EMPL", "EID", "STAFF"})  # generic English, not specific to any file


def learn_staff_prefixes(docs: list[Document]) -> frozenset[str]:
    """Which code prefixes identify people in this set of documents.

    "EMP-12345" and "REF-4488" have the same shape; only one is a person. The
    documents themselves say which: a code under a column headed "Emp ID" or
    "Vendor ID", or in brackets straight after a name, is a person's. Prefixes
    seen there are learned and then recognised everywhere, including in prose
    with no label at all.
    """
    seen: dict[str, int] = {}
    for doc in docs:
        by_id = {b.block_id: b for b in doc.blocks}
        for block in doc.blocks:
            pattern = _CODE_OCR if block.source is Source.OCR else _CODE
            header = by_id[block.header_id].text if block.header_id else ""
            under_label = bool(_STAFF_LABEL.search(header))
            for m in pattern.finditer(block.text):
                if under_label or _AFTER_NAME.search(block.text[: m.start()]):
                    seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    # One sighting could be an OCR fragment; a real scheme shows up repeatedly.
    return _SEED_PREFIXES | {prefix for prefix, count in seen.items() if count >= 3}


def _staff_ids(ctx: _Context) -> Iterator[Hit]:
    for m in (_CODE_OCR if ctx.ocr else _CODE).finditer(ctx.text):
        if m.group(1) not in ctx.staff_prefixes:
            continue
        labelled = bool(_STAFF_LABEL.search(ctx.label)) or bool(_AFTER_NAME.search(ctx.text[: m.start()]))
        yield Hit(m.start(), m.end(), "STAFF_ID", "staff_id_prefix", 0.9 if labelled else 0.8, labelled=labelled)


# --- names written as initials and a surname, inside pictures ------------------

_INITIALS_NAME = re.compile(r"(?<![A-Za-z])(?:[A-Z]{1,2}\.[^\S\n]?|[A-Z][^\S\n]){1,3}[A-Z][a-z]{2,}(?:[-'’][A-Z][a-z]+)?")


def _initialled_names(ctx: _Context) -> Iterator[Hit]:
    """ "J. Q. Sample" in a picture, as OCR leaves it: "JQ. Sample", "J. Q Sample", "J.Q.Sample".

    Names are the language model's job, but it reads a line of a picture as a
    string of half-garbled cells and often lets this form go. The form itself
    is distinctive enough to report: initials, at least one with its full
    stop, then a capitalised word that passes the test every name must pass
    and is not an ordinary word of this document ("Appendix A. Scales" is a
    heading). Only inside pictures, where a false alarm costs a grey box.
    """
    if not ctx.picture:
        return
    for m in _INITIALS_NAME.finditer(ctx.text):
        surname = re.split(r"[.\s]+", m.group())[-1]
        if surname.lower() in ctx.common or surname.split("-")[0].lower() in ctx.common:
            continue
        if "." in m.group() and allowlist.plausible_person(re.sub(r"\.(?=\S)", ". ", m.group())):
            yield Hit(m.start(), m.end(), "PERSON", "initials_and_surname", 0.6)


_RULES = (_emails, _phones, _ssn_and_tax, _cards, _pans, _pesels, _passports, _births, _staff_ids, _initialled_names)
_NO_TEXT = {BlockType.TABLE, BlockType.SLIDE, BlockType.GROUP}


def detect(docs: list[Document]) -> list[Finding]:
    """Run every rule over every block of every document.

    The documents are taken together because staff-ID prefixes learned in one
    file apply to the others.
    """
    prefixes = learn_staff_prefixes(docs)
    domains = learn_domains(docs)
    findings: list[Finding] = []
    for doc in docs:
        by_id = {b.block_id: b for b in doc.blocks}
        row_labels = _row_labels(doc)
        above = _lines_above(doc)
        common = allowlist.common_words(doc)
        for block in doc.blocks:
            if block.type in _NO_TEXT or not block.text.strip():
                continue
            header = by_id[block.header_id].text if block.header_id else ""
            label = f"{header} {row_labels.get(block.block_id, '')} {above.get(block.block_id, '')}"
            picture = block.container is Container.IMAGE
            ctx = _Context(block.text, label, block.source is Source.OCR, prefixes, picture, domains, common)
            where = (("ocr",) if ctx.ocr else ()) + (("quarantined",) if block.quarantined else ())
            for rule in _RULES:
                for hit in rule(ctx):
                    findings.append(
                        make(
                            doc, block, hit.start, hit.end,
                            entity=hit.entity, detector="rules", rule=hit.rule,
                            confidence=hit.confidence, subtype=hit.subtype,
                            flags=(("keyword",) if hit.labelled else ()) + hit.flags + where,
                            location=location_of(block, hit.start),
                        )
                    )
    return resolve_overlaps(findings)


def _lines_above(doc: Document) -> dict[str, str]:
    """For each line read inside a picture, the text of the line directly above it.

    On a card or a form the label is printed above its value ("DATE OF BIRTH",
    then the date on the next line), so the keyword a rule looks for is not in
    the same line. Only a short line with no digits counts as a label, and only
    from the same OCR engine, close above, and overlapping sideways.
    """
    lines: dict[tuple, list[Block]] = {}
    for block in doc.blocks:
        if block.container is Container.IMAGE and block.parent_id and block.bbox is not None and block.text.strip():
            lines.setdefault((block.parent_id, (block.native_ref or {}).get("engine")), []).append(block)
    out: dict[str, str] = {}
    for group in lines.values():
        for block in group:
            height = block.bbox.y1 - block.bbox.y0
            best = None
            for other in group:
                if other is block or len(other.text.split()) > 4 or any(c.isdigit() for c in other.text):
                    continue  # a label is a few words with no digits; a line of values is not a label
                gap = block.bbox.y0 - other.bbox.y1
                sideways = min(block.bbox.x1, other.bbox.x1) - max(block.bbox.x0, other.bbox.x0)
                if -0.3 * height <= gap <= 1.5 * height and sideways > 0 and (best is None or gap < best[0]):
                    best = (gap, other.text)
            if best is not None:
                out[block.block_id] = best[1]
    return out


def _row_labels(doc: Document) -> dict[str, str]:
    """For each table cell, the text of the first cell in its row.

    In a two-column "field | value" table the label sits to the left of the
    value, not above it.
    """
    first: dict[tuple[str, int], Block] = {}
    for block in doc.blocks:
        if block.type is BlockType.TABLE_CELL:
            key = (block.parent_id, block.row)
            if key not in first or block.col < first[key].col:
                first[key] = block
    return {
        b.block_id: first[(b.parent_id, b.row)].text
        for b in doc.blocks
        if b.type is BlockType.TABLE_CELL and first[(b.parent_id, b.row)] is not b
    }
