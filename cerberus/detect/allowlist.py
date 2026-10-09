"""Precision filters: things that look like PII to a detector but are not.

A model asked for "person" will also offer job titles, field labels and
organisation names, because they sit where names sit ("Owner: Chief Risk
Officer"). These checks remove them after detection. They are deliberately
simple and explainable: a shape test and a word list.

Nothing here is specific to one organisation. Names of particular companies
and products are handled separately (the model is asked to label organisations
so they do not get labelled as people).
"""

from __future__ import annotations

import re

# Words that occur in job titles, role names and field labels but not in
# personal names. A span containing any of them is not accepted as a person.
NOT_NAME_WORDS = frozenset("""
    chief officer executive director manager head lead vp svp evp ceo cfo coo cio cro cto ciso dpo
    president chair chairman chairperson counsel secretary treasurer auditor analyst engineer
    administrator admin owner approver reviewer requester requestor respondent assessor sponsor
    contact vendor vendors supplier provider client customer employee staff member team group
    committee board division department office function unit desk
    name names role roles title type types status category field label value number id
    primary secondary new general senior junior deputy assistant associate interim acting
    risk compliance security information data protection privacy audit finance legal operations
    technology engineering infrastructure analytics governance assurance resilience
    third party third-party international internal external named the and of for to
    workflow workflows template templates attribute attributes control controls policy
    business owners managers officers contacts appendix section figure table page annex
""".split())

# Lower-case words allowed inside a name.
_PARTICLES = frozenset("de del della der di da dos du la le van von bin binti al el ibn ter ten".split())


def plausible_person(text: str) -> bool:
    """Does this span have the shape of a personal name?

    Two to five words; each a capitalised word, an initial or a particle; at
    least one capitalised word; no job-title or label word.

    A single word is not accepted on its own: one capitalised word is as often
    a product, a company or the start of a sentence as it is a first name.
    People referred to by one name are caught by the dictionary sweep instead,
    once their full name has been seen somewhere.
    """
    words = text.replace("\n", " ").split()
    if not 2 <= len(words) <= 5:
        return False
    proper = 0
    for word in words:
        bare = word.strip(".,;:()")
        if not bare:
            return False
        if bare.lower() in NOT_NAME_WORDS:
            return False
        if bare in _PARTICLES:
            continue
        if _is_initial(bare):
            continue
        if not _is_name_word(bare):
            return False
        proper += 1
    return proper >= 1


def _is_initial(word: str) -> bool:
    letters = word.replace(".", "")
    return 1 <= len(letters) <= 2 and letters.isalpha() and letters.isupper()


def _is_name_word(word: str) -> bool:
    """Capitalised, alphabetic, not all capitals; hyphens and apostrophes join parts."""
    parts = [p for p in word.replace("'", "-").replace("’", "-").split("-") if p]
    return bool(parts) and all(
        p.isalpha() and p[0].isupper() and (len(p) == 1 or not p.isupper()) for p in parts
    )


_STREET = (
    r"street|st|road|rd|avenue|ave|lane|ln|drive|dr|court|ct|boulevard|blvd|way|place|pl|crescent|"
    r"close|square|terrace|parkway|highway|suite|apartment|apt|flat|unit|floor"
)
_STREET_LINE = re.compile(rf"\b\d+[A-Za-z]?\b[^,;]{{0,40}}\b(?:{_STREET})\b\.?", re.IGNORECASE)
_POSTCODE = re.compile(
    r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"  # US state and ZIP
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"  # UK postcode
    r"|\b\d{2}-\d{3}\b"  # Polish postcode
    r"|,\s*\d{5,6}\b"  # a 5 or 6 digit code after a comma (India, Germany, France...)
)


def plausible_address(text: str) -> bool:
    """Does this span have the shape of a postal address?

    It must contain a street line (a number followed by a street word) or a
    postcode. A place name with a number near it ("Sample City 24F", "Appendix 4")
    is what a model offers when asked for addresses, and is not one.
    """
    return bool(_STREET_LINE.search(text) or _POSTCODE.search(text))


_NAME_RUN = re.compile(r"(?:\b[A-Z][A-Za-z'’-]*\.?[^\S\n]+){1,4}[A-Z][A-Za-z'’-]+")
_CODE = re.compile(r"\S*(?:@|(?:\d[^\d\s]*){5})\S*")  # one word: an "@", or five digits
_NUMBER_RUN = re.compile(r"[+(]?\d[\d ().\-/]{2,}\d")  # digits in groups: a phone number
_RUN_DIGITS = 7  # fewer digits than this, in groups, is a reference or a quantity, not a phone number


_WRITTEN_DATE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?[ .,/-]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?[ .,/-]+\d{2,4}\b",
    re.IGNORECASE,
)


_INITIALS = re.compile(r"\b(?:[A-Z]\.[^\S\n]?){1,3}[A-Z][A-Za-z'’-]{2,}")  # "J. Q. Sample", also "J.Q.Sample"


def shaped_like_pii(text: str, common: frozenset[str] = frozenset()) -> list[tuple[int, int]]:
    """Spans that have the shape of personal data, whether or not a detector reported them.

    Used only inside pictures, where the cost of a miss is a value left
    visible and the cost of a false alarm is a grey box over a harmless word.
    The shapes: a word with an "@"; a word with five or more digits (a staff
    or government number, a numeric date); seven or more digits in groups (a
    phone number); a date written with the month's name; initials followed by
    a surname; and a run of capitalised words that passes the same test a
    detected name must pass.

    common is a set of ordinary words, in small letters (see
    redact.office.common_words). A run made only of those is a heading or a
    label ("Customer Complaint"), not a name, and ordinary words at either
    end of a run are not part of the name ("High Jane Sample" in a table row).

    Deliberately not a shape: a short code such as a risk or ticket reference
    (three letters and four digits), or a year. Covering those hid most of a
    form to protect nothing.
    """
    def ordinary(word: str) -> bool:
        letters = re.sub(r"[^A-Za-z]", "", word).lower()
        return len(letters) > 1 and letters in common

    spans = [match.span() for match in _CODE.finditer(text)]
    spans += [m.span() for m in _NUMBER_RUN.finditer(text) if sum(c.isdigit() for c in m.group()) >= _RUN_DIGITS]
    spans += [match.span() for match in _WRITTEN_DATE.finditer(text)]
    spans += [m.span() for m in _INITIALS.finditer(text) if not ordinary(re.split(r"\.", m.group())[-1])]
    for match in _NAME_RUN.finditer(text):
        words = list(re.finditer(r"\S+", match.group()))
        if all(ordinary(w.group()) for w in words):
            continue
        while len(words) > 2 and ordinary(words[0].group()):
            words.pop(0)
        while len(words) > 2 and ordinary(words[-1].group()):
            words.pop()
        # A title in front of a name ("Owner J. Q. Sample") must not hide the name:
        # try the run without its first words, then without its last, longest first.
        pieces = sorted(
            ((words[i].start(), words[j].end()) for i in range(len(words)) for j in range(i + 1, len(words))),
            key=lambda piece: piece[0] - piece[1],
        )
        for start, end in pieces:
            if plausible_person(match.group()[start:end]):
                spans.append((match.start() + start, match.start() + end))
                break
    return sorted(spans)
