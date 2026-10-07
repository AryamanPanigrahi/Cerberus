"""Dictionary sweep: a name found once is searched for everywhere.

The model finds each person somewhere, but not every time: the same name it
caught in a sentence it may miss in a table, in capitals, or shortened to
"T. Surname". The sweep closes that gap. Every name the model found, in any
file, goes into a dictionary; every file is then searched for those names and
for the forms a name is usually written in.

This is the step that makes detection consistent, which the brief asks for:
once a person is known, every mention is treated the same way in every file.

Forms searched, for a name "First M. Last":

    First M. Last      as found
    First Last         middle initial dropped
    F. Last, F. M. Last, F.M. Last     abbreviated
    Last, First        directory order

A surname or first name on its own is NOT swept. Different people share
surnames, and many surnames are ordinary words; matching them alone would
both link the wrong people and flag innocent text.

Not every name the model proposes is swept for. A name needs independent
evidence first (see candidates), because the sweep would repeat a mistake on
every page.

Text that came from OCR is also matched approximately, because OCR drops a
space or swaps a letter inside a name.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

from ..extract.ir import BlockType, Document, Source
from . import allowlist
from .finding import Finding, location_of, make

_NO_TEXT = {BlockType.TABLE, BlockType.SLIDE, BlockType.GROUP}
_FUZZY = 0.88  # how alike an OCR'd name must be to a known one
_WORD = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?")
_HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam"})
_MIN_OCR = 0.6  # a name on a word the OCR was less sure of than this cannot seed the sweep
_PERSON_HEADER = re.compile(
    r"\b(?:name|employee|member|owner|manager|officer|executive|lead|chair|holder|author|contact|"
    r"approver|incumbent|requestor|requester|sponsor|attendee|reviewer|assignee)\b",
    re.IGNORECASE,
)


def name_forms(name: str) -> dict[str, str]:
    """The written forms of one full name, each mapped to the rule that explains it."""
    words = [w for w in name.replace(",", " ").split() if w.rstrip(".").lower() not in _HONORIFICS]
    if not words:
        return {}
    is_initial = [len(w.rstrip(".")) <= 2 and w.rstrip(".").isupper() for w in words]
    given = [w for w, initial in zip(words[:-1], is_initial[:-1]) if not initial]
    last = words[-1]
    forms = {" ".join(words): "name_exact"}
    if not given or is_initial[-1]:
        return forms  # already abbreviated, or no usable first name: nothing safe to derive
    first = given[0]
    initials = [w[0] for w in words[:-1]]
    forms.setdefault(f"{first} {last}", "name_without_middle")
    forms.setdefault(f"{last}, {first}", "name_reversed")
    forms.setdefault(f"{first[0]}. {last}", "name_initial")
    if len(initials) > 1:
        forms.setdefault(" ".join(f"{i}." for i in initials) + f" {last}", "name_initial")
    return forms


def candidates(docs: list[Document], findings: list[Finding]) -> dict[str, tuple[str, ...]]:
    """Every name that could seed the sweep, with the independent evidence for it.

    A sweep multiplies whatever it is given: one wrong seed is found again on
    every page. So a name the model proposed is only swept for if something
    other than the model's own opinion backs it up. The kinds of evidence:

    - email:         an e-mail address found by the rules carries its surname
    - staff_id:      a staff ID sits in the same block or the same table row
    - header:        it is in a table column whose header names a person's role
    - file_property: it is the value of an author-type file property
    - repeated:      the model found it in at least two places (a sighting in a
                     picture counts here, once there is a clean one elsewhere)

    A name with no evidence is searched for only exactly as it was written. Its
    abbreviated and approximate forms are not, because those are the forms that
    turn one mistake into many. Exact repeats must be caught regardless: text
    the pipeline already believes is a name cannot be left in the output.

    Never eligible, whatever the evidence: names read out of pictures or
    quarantined regions, names in OCR'd running headers and footers, and names
    sitting on a word the OCR itself was unsure of. Those are where name-shaped
    junk comes from.
    """
    blocks = {b.block_id: b for doc in docs for b in doc.blocks}
    email_words: set[str] = set()
    id_blocks: set[str] = set()
    id_rows: set[tuple[str, int]] = set()
    for f in findings:
        if f.entity == "EMAIL":
            local = f.text.split("@")[0].lower()
            parts = [w for w in re.split(r"[^a-z]+", local) if w]
            email_words.update(parts)
            email_words.add("".join(parts))
        elif f.entity == "STAFF_ID":
            id_blocks.add(f.block_id)
            block = blocks.get(f.block_id)
            if block is not None and block.type is BlockType.TABLE_CELL:
                id_rows.add((block.parent_id, block.row))

    evidence: dict[str, set[str]] = {}
    sightings: dict[str, int] = {}
    display: dict[str, str] = {}
    anywhere: dict[str, int] = {}
    for f in findings:
        if f.entity != "PERSON" or f.detector == "dictionary":
            continue
        name = " ".join(f.text.split())
        if not allowlist.plausible_person(name):
            continue
        anywhere[_key(name)] = anywhere.get(_key(name), 0) + 1
        if f.location == "image" or "quarantined" in f.flags:
            continue
        block = blocks.get(f.block_id)
        if block is not None:
            if block.type in (BlockType.HEADER, BlockType.FOOTER) and block.source is Source.OCR:
                continue
            read = [w.conf for w in block.words if w.start < f.end and f.start < w.end]
            if read and min(read) < _MIN_OCR:
                continue
        key = _key(name)
        display.setdefault(key, name)
        found = evidence.setdefault(key, set())
        sightings[key] = sightings.get(key, 0) + 1
        surname = re.sub(r"[^a-z]", "", name.split()[-1].lower())
        if surname and surname in email_words:
            found.add("email")
        if block is not None:
            in_row = block.type is BlockType.TABLE_CELL and (block.parent_id, block.row) in id_rows
            if block.block_id in id_blocks or in_row:
                found.add("staff_id")
            header = blocks.get(block.header_id) if block.header_id else None
            if header is not None and _PERSON_HEADER.search(header.text):
                found.add("header")
            ref = block.native_ref or {}
            if block.type is BlockType.METADATA and (
                ref.get("core_property") in ("author", "last_modified_by")
                or ref.get("info") == "author"
                or ref.get("xmp") in ("creator", "li")
            ):
                found.add("file_property")
    for key in sightings:
        if anywhere[key] >= 2:
            evidence[key].add("repeated")
    return {display[key]: tuple(sorted(found)) for key, found in evidence.items()}


def read_review(path: str | Path) -> tuple[frozenset[str], frozenset[str]]:
    """A person's decisions on the name list: (keys to keep, keys to drop)."""
    keep, drop = set(), set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() in ("keep", "drop"):
            (keep if parts[0].strip() == "keep" else drop).add(_key(parts[1]))
    return frozenset(keep), frozenset(drop)


def write_review(path: str | Path, names: dict[str, tuple[str, ...]]) -> None:
    """Write the name list for an optional check by a person.

    Each line starts with what the pipeline decided on its own:

        keep    there is evidence: searched for in every written form
        exact   no evidence: searched for only exactly as written

    A person may change any line to "drop" (not a person: removed everywhere)
    or to "keep". With no edits, re-running gives the same result as before.
    """
    lines = [
        "# One name per line: decision, name, evidence (tab-separated).",
        "# keep = searched for in every written form; exact = only exactly as written.",
        "# Change a line to 'drop' if it is not a person, or to 'keep' to search for all its forms. Save and re-run.",
    ]
    for name, found in sorted(names.items(), key=lambda item: (bool(item[1]), item[0].lower())):
        lines.append(f"{'keep' if found else 'exact'}\t{name}\t{', '.join(found) or 'no supporting evidence'}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def detect(
    docs: list[Document],
    known: list[Finding],
    keep: frozenset[str] = frozenset(),
    drop: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Search every document for the supported names in `known` and their written forms.

    keep and drop are a person's overrides (see write_review); both are optional.
    """
    forms: dict[str, str] = {}
    derived: set[str] = set()  # forms that may also be matched approximately
    for name, found in candidates(docs, known).items():
        if _key(name) in drop:
            continue
        if found or _key(name) in keep:
            for form, rule in name_forms(name).items():
                forms.setdefault(form, rule)
                derived.add(form)
        else:
            forms.setdefault(name, "name_exact")  # literal repeats only
    if not forms:
        return []
    # Longest first, so "First M. Last" is preferred to the "First" + "Last" inside it.
    ordered = sorted(forms, key=len, reverse=True)

    def compiled(chosen: list[str], flags: int) -> re.Pattern | None:
        body = "|".join(_flexible(form) for form in chosen)
        return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])", flags) if chosen else None

    # A form with an initial in it is matched in its exact case: "A. Park" must
    # not match the words "a park". Forms made only of full words ignore case,
    # so a name set in capitals is still found.
    has_initial = [f for f in ordered if any(len(w.rstrip(".")) <= 2 for w in f.replace(",", "").split())]
    full_words = [f for f in ordered if f not in set(has_initial)]
    patterns = [p for p in (compiled(full_words, re.IGNORECASE), compiled(has_initial, 0)) if p]
    canonical = {_key(form): form for form in ordered}
    by_initial: dict[str, list[str]] = {}
    for form in ordered:
        if form in derived:
            by_initial.setdefault(form[0].lower(), []).append(form.lower())
            by_initial.setdefault("last:" + form.split()[-1][0].lower(), []).append(form.lower())
    longest = max((len(form.split()) for form in derived), default=0)

    findings: list[Finding] = []
    for doc in docs:
        for block in doc.blocks:
            if block.type in _NO_TEXT or not block.text.strip():
                continue
            ocr = block.source is Source.OCR
            flags = (("ocr",) if ocr else ()) + (("quarantined",) if block.quarantined else ())
            taken: list[tuple[int, int]] = []
            here: dict[tuple[int, int], Finding] = {}
            matches = [m for pattern in patterns for m in pattern.finditer(block.text)]
            for m in sorted(matches, key=lambda m: (m.start() - m.end(), m.start())):  # longest first
                if any(a < m.end() and m.start() < b for a, b in taken):
                    continue
                form = canonical.get(_key(m.group()))
                rule = forms.get(form, "name_exact")
                confidence = 0.9 if rule == "name_exact" else 0.85
                here[(m.start(), m.end())] = _finding(doc, block, m.start(), m.end(), rule, confidence, flags)
                taken.append((m.start(), m.end()))
            if ocr:
                for start, end, swallowed in list(_fuzzy(block.text, by_initial, longest, taken)):
                    for span in swallowed:
                        here.pop(span, None)
                    # Rated above a bare exact match: it is the longer, more complete name.
                    here[(start, end)] = _finding(doc, block, start, end, "name_approximate", 0.92, flags)
            findings += here.values()
    return sorted(findings, key=lambda f: (f.doc_id, f.block_id, f.start))


def _finding(doc, block, start, end, rule, confidence, flags) -> Finding:
    return make(
        doc, block, start, end, entity="PERSON", detector="dictionary", rule=rule,
        confidence=confidence, flags=flags, location=location_of(block, start),
    )


def _flexible(form: str) -> str:
    """A form as a pattern: any white space between words; the dot after an
    initial may be missing, and so may the space after it ("P.S. Last")."""
    out = ""
    for word in form.split():
        if word.endswith("."):
            out += re.escape(word[:-1]) + r"\.?\s*"
        else:
            out += re.escape(word) + r"\s+"
    return out.removesuffix(r"\s+").removesuffix(r"\s*")


def _key(text: str) -> str:
    return " ".join(text.replace(".", " ").lower().split())


def _fuzzy(text: str, by_initial: dict[str, list[str]], longest: int, taken: list[tuple[int, int]]):
    """Runs of words in OCR text that are nearly, but not exactly, a known name.

    Candidates are looked up by the first letter of the run and of its last
    word, so a misread first letter does not hide the match. A run may contain
    something already matched (the "Q. Sample" inside a misread "Jane Q.
    Sample"): the longer run is the name, so it is kept and yielded with the
    spans it swallows.
    """
    words = [(m.start(), m.end(), m.group()) for m in _WORD.finditer(text)]
    claimed: list[tuple[int, int]] = []
    for size in range(longest, 1, -1):
        for i in range(len(words) - size + 1):
            start, end = words[i][0], words[i + size - 1][1]
            if any(a < end and start < b for a, b in claimed):
                continue
            overlapping = [(a, b) for a, b in taken if a < end and start < b]
            if any(not (start <= a and b <= end) or (a, b) == (start, end) for a, b in overlapping):
                continue  # equal to, or only partly over, something already matched
            # Words must be neighbours on one line, not picked from across the text.
            if any(words[k + 1][0] - words[k][1] > 2 for k in range(i, i + size - 1)):
                continue
            window = text[start:end].lower()
            pool = by_initial.get(window[0], []) + by_initial.get("last:" + words[i + size - 1][2][0].lower(), [])
            for candidate in dict.fromkeys(pool):
                if abs(len(window) - len(candidate)) > 2:
                    continue
                if SequenceMatcher(None, window, candidate, autojunk=False).ratio() >= _FUZZY:
                    claimed.append((start, end))
                    yield start, end, overlapping
                    break
