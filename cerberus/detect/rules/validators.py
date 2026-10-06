"""Checks that tell a real identifier from a string of digits that merely looks like one.

A pattern alone ("eleven digits") matches order numbers, phone numbers and
serials. Each identifier scheme has structure built in: a checksum, a date, a
restricted character. Testing that structure is the main false-positive control.
"""

from __future__ import annotations

from datetime import date


def luhn(digits: str) -> bool:
    """Card numbers: the Luhn check digit. One random digit string in ten passes,
    so this is necessary but not sufficient."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return bool(digits) and total % 10 == 0


def pesel_birth_date(first_six: str) -> date | None:
    """PESEL (Poland): the first six digits are the birth date, YYMMDD, with the
    century folded into the month (+20 for the 2000s, +80 for the 1800s...)."""
    year, month, day = int(first_six[:2]), int(first_six[2:4]), int(first_six[4:6])
    century = {0: 1900, 20: 2000, 40: 2100, 60: 2200, 80: 1800}.get(month // 20 * 20)
    if century is None:
        return None
    try:
        return date(century + year, month % 20, day)
    except ValueError:
        return None


def pesel(digits: str) -> bool:
    """PESEL: a valid embedded birth date and a matching check digit."""
    if len(digits) != 11 or pesel_birth_date(digits[:6]) is None:
        return False
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    check = (10 - sum(int(d) * w for d, w in zip(digits, weights)) % 10) % 10
    return check == int(digits[10])


# PAN (India): the fourth character says what kind of holder it is.
PAN_HOLDERS = {
    "P": "individual", "C": "company", "H": "family", "F": "firm", "A": "association",
    "T": "trust", "B": "body of individuals", "L": "local authority",
    "J": "juridical person", "G": "government",
}


def pan_holder(value: str) -> str | None:
    """PAN: five letters, four digits, one letter; the fourth letter must be a holder type."""
    return PAN_HOLDERS.get(value[3]) if len(value) == 10 else None


def ssn_flags(area: str, group: str, serial: str) -> tuple[str, ...] | None:
    """US SSN structure. None means "cannot be an SSN".

    Area 000 and 666, group 00 and serial 0000 are never issued. Areas 900-999
    are never issued as SSNs either, but teaching and test data use exactly that
    range so it cannot collide with a real person; rejecting it would miss
    every SSN in such a document. It is accepted and flagged instead.
    """
    if area in ("000", "666") or group == "00" or serial == "0000":
        return None
    return ("test_range",) if area >= "900" else ()
