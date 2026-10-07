"""Entity resolution and stable tokens.

Detection gives a list of separate findings. This step decides which findings
are the same thing and gives each thing one token, such as <PERSON_3> or
<EMAIL_7>, used for every mention in every file.

Tokens, not black bars: with a token in place of each name the downstream
model can still follow who did what across a document set, without ever
seeing who they are.

How "the same thing" is decided:

- Values (e-mails, phones, IDs) are compared after normalising how they are
  written: case, spacing and punctuation do not make two values different.
- People are compared by full name. Written forms of one name ("Jane Q.
  Sample", "Jane Sample", "J. Sample", "JANE SAMPLE") resolve to one person.
  A surname alone never links two findings: different people share surnames.
  An abbreviated form is linked only if exactly one known person fits it.

The vault (which value got which token) can be saved and reloaded so tokens
stay the same from one run to the next. It holds the original values, so it
lives only in the gitignored outputs folder.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from ..detect.finding import Finding

_HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "madam"})
_NEAR = 0.88  # how alike a scanned name must be to a known one to be the same person


@dataclass(frozen=True)
class Tokenized:
    finding: Finding
    token: str  # e.g. "<PERSON_3>"
    key: str  # the normalised identity the token stands for


@dataclass
class Vault:
    """Which identity has which token. Reloading it keeps tokens stable across runs."""

    tokens: dict[str, dict[str, str]] = field(default_factory=dict)  # entity -> key -> token
    values: dict[str, list[str]] = field(default_factory=dict)  # token -> original values seen

    def token_for(self, entity: str, key: str) -> str:
        known = self.tokens.setdefault(entity, {})
        if key not in known:
            known[key] = f"<{entity}_{len(known) + 1}>"
        return known[key]

    def remember(self, token: str, value: str) -> None:
        seen = self.values.setdefault(token, [])
        if value not in seen:
            seen.append(value)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps({"tokens": self.tokens, "values": self.values}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> Vault:
        if not Path(path).exists():
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("tokens", {}), data.get("values", {}))


def assign(findings: list[Finding], vault: Vault | None = None) -> tuple[list[Tokenized], Vault]:
    """Give every finding a token; the same identity gets the same token everywhere.

    Findings are taken in a fixed order (file, block, position), so numbering
    is the same on every run over the same files.
    """
    vault = vault or Vault()
    ordered = sorted(findings, key=lambda f: (f.doc_id, f.block_id, f.start))
    people = _resolve_people([(f.text, "ocr" in f.flags) for f in ordered if f.entity == "PERSON"])
    out = []
    for f in ordered:
        key = people[_flat(f.text)] if f.entity == "PERSON" else _value_key(f)
        token = vault.token_for(f.entity, key)
        vault.remember(token, " ".join(f.text.split()))
        out.append(Tokenized(f, token, key))
    return out, vault


def image_only(tokenized: list[Tokenized]) -> list[str]:
    """Tokens whose every mention is inside a picture: found nowhere in ordinary text."""
    places: dict[str, set[str]] = {}
    for item in tokenized:
        places.setdefault(item.token, set()).add(item.finding.location)
    return sorted(token for token, where in places.items() if where == {"image"})


# --- values -----------------------------------------------------------------


def _value_key(f: Finding) -> str:
    """One spelling for each value, so that formatting does not split an identity."""
    text = f.text.strip()
    if f.entity == "EMAIL":
        return re.sub(r"\s+", "", text).replace("|", "l").lower()
    if f.entity in ("PHONE", "SSN", "TAX_ID", "CARD"):
        digits = re.sub(r"\D", "", text)
        prefix = f"{f.subtype}:" if f.subtype else ""  # "last four digits" is not the full number
        return prefix + ("+" if text.startswith("+") else "") + digits
    if f.entity == "STAFF_ID":  # OCR writes "ABC- 123" or "ABC.123" for "ABC-123"
        return re.sub(r"[\s.]+", "-", text.upper()).replace("--", "-")
    if f.entity in ("PAN", "PESEL", "PASSPORT"):
        return re.sub(r"\s+", "", text.upper())
    return " ".join(text.lower().split())  # dates, addresses


# --- people -----------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.replace(",", " ").split())


@dataclass
class _Name:
    first: str  # "" when the name has no full first name
    initial: str  # first letter of the first name or initial
    middle: tuple[str, ...]  # middle initials
    last: str

    @classmethod
    def parse(cls, text: str) -> _Name | None:
        # A dot ends a word, so "J.Q. Sample" reads as J, Q, Sample.
        words = [w.strip(".").lower() for w in _flat(text.replace(".", ". ")).split()]
        words = [w for w in words if w and w not in _HONORIFICS]
        if len(words) < 2:
            return None  # a single word: not enough to say who it is
        # One or two letters before the surname are initials, not a first name.
        # OCR often glues them together ("JQ Sample"), and treating "jq" as a
        # first name would invent a second person for the abbreviation to fit.
        head = []
        for word in words[:-1]:
            head += list(word) if len(word) <= 2 else [word]
        first = head[0] if len(head[0]) > 1 else ""
        return cls(first, head[0][0], tuple(w[0] for w in head[1:]), words[-1])

    def fits(self, other: _Name) -> bool:
        """Could these be one person? Same surname and first initial, and
        middle initials that do not contradict each other."""
        if self.last != other.last or self.initial != other.initial:
            return False
        if self.first and other.first and self.first != other.first:
            return False
        return not self.middle or not other.middle or self.middle == other.middle


def _resolve_people(names: list[tuple[str, bool]]) -> dict[str, str]:
    """Map every written name to the identity key of the person it refers to.

    names: (text, came from OCR). Only scanned text is matched approximately:
    a typed name one letter different from another is a different person, but
    a scanned one is probably a misreading.
    """
    scanned = {_flat(n) for n, ocr in names if ocr}
    written = list(dict.fromkeys(_flat(n) for n, _ in names))  # distinct, in order of appearance
    parsed = {w: _Name.parse(w) for w in written}

    def key_of(person: _Name) -> str:
        return " ".join([person.first, *person.middle, person.last])

    def near(w: str, people: list[_Name]) -> list[_Name]:
        if w not in scanned:
            return []
        flat = " ".join(w.lower().replace(".", " ").split())
        return [p for p in people if SequenceMatcher(None, flat, key_of(p), autojunk=False).ratio() >= _NEAR]

    # 1. Full names found: each distinct (first, middle?, last) is a person.
    #    "Jane Sample" joins "Jane Q. Sample" if only one Jane ... Sample exists.
    people: list[_Name] = []
    for w in written:
        name = parsed[w]
        if name is None or not name.first:
            continue
        match = [p for p in people if p.fits(name)]
        if len(match) == 1:
            if name.middle and not match[0].middle:
                match[0].middle = name.middle  # learn the middle initial
        elif not match and len(near(w, people)) != 1:
            people.append(_Name(name.first, name.initial, name.middle, name.last))

    result: dict[str, str] = {}
    for w in written:
        name = parsed[w]
        if name is None:
            result[w] = "single:" + w.lower()
            continue
        match = [p for p in people if p.fits(name)]
        if len(match) == 1:
            result[w] = key_of(match[0])
            continue
        if not match:
            # Scanned text: a swapped letter should not create a new person.
            alike = near(w, people)
            if len(alike) == 1:
                result[w] = key_of(alike[0])
                continue
        # Nobody fits, or more than one person does: keep it apart rather than guess.
        result[w] = "unresolved:" + w.lower().replace(".", "")
    return result
