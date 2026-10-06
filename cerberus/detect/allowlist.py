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
