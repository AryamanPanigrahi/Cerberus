"""Read the text inside a picture embedded in a document.

Screenshots, badges and scanned forms pasted into a DOCX, PPTX or typed PDF
hold text that exists only as pixels. Each picture is OCR'd and its lines are
emitted as children of its image region, marked as image content.

- The same picture is often used many times (a logo on every slide). It is
  read once and the result reused, keyed by a hash of its bytes.
- Vector formats (EMF, WMF, SVG) cannot be opened as pixels, so they are
  converted to PNG with LibreOffice first.
- A picture that cannot be opened or converted is quarantined, as is one whose
  text reads below the confidence threshold: unreadable is not the same as empty.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from PIL import Image, UnidentifiedImageError

from . import figures, layout, ocr
from .ir import BBox, Block, BlockType, Container, Document, Source, Unit, Word

MIN_IMAGE_CONFIDENCE = 0.60  # starting value, shared with figures on scanned pages
_MIN_SIDE = 40  # pixels; anything smaller is an icon or a bullet, not text
_TEXT_HEIGHT = 36  # pixels; the word height OCR reads best, about 11pt print at 300 dpi
_VECTOR = {
    "image/x-emf": ".emf", "image/emf": ".emf",
    "image/x-wmf": ".wmf", "image/wmf": ".wmf",
    "image/svg+xml": ".svg",
}
_WINDOWS_SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")


@dataclass
class Reading:
    lines: list[layout.Line] = field(default_factory=list)  # words in the picture's own pixels
    conf: float | None = None  # None: no text found
    size: tuple[int, int] | None = None
    error: str = ""  # set when the picture could not be read at all


def best_reading(image: Image.Image) -> list[ocr.OcrWord]:
    """OCR under several segmentation modes and keep the best reading.

    No single mode suits every picture: a form reads best as one block (6), a
    badge or card as a column (4), a diagram as scattered text (11). The best
    reading is the one with the most confidently read characters.
    """
    def score(words):
        return sum(w.conf * len(w.text) for w in words if w.conf >= 0.6)

    return max((ocr.ocr_image(image, psm=mode) for mode in (6, 4, 11)), key=score)


def read_pixels(gray: Image.Image, border: int = 0) -> list[ocr.OcrWord]:
    """The words in a greyscale picture, positioned in that picture's pixels.

    The picture is read twice. A first quick pass measures how tall its text
    is; the picture is then enlarged (or left alone) so that text comes out at
    the height OCR reads best, and read properly. If the first pass finds
    nothing it can trust, the text is probably tiny, and the enlargement is
    guessed from the picture's width instead.
    """
    probe, scale, _, _ = figures.prepare(gray, border, scale=1)
    heights = [w.height for w in ocr.ocr_image(probe, psm=11) if w.conf >= 0.6 and len(w.text) > 2]
    wanted = round(_TEXT_HEIGHT / median(heights)) if heights else None
    image, scale, left, top = figures.prepare(gray, border, scale=wanted)
    return [
        ocr.OcrWord(
            w.text, left + w.x0 / scale, top + w.y0 / scale,
            left + w.x1 / scale, top + w.y1 / scale, w.conf, w.line,
        )
        for w in best_reading(image)
    ]


class Reader:
    """Reads pictures for one document, remembering those already seen."""

    def __init__(self):
        self._seen: dict[str, Reading] = {}

    def read(self, blob: bytes, content_type: str | None = None) -> Reading:
        key = hashlib.sha1(blob).hexdigest()
        if key not in self._seen:
            self._seen[key] = _read(blob, content_type)
        return self._seen[key]


def _read(blob: bytes, content_type: str | None) -> Reading:
    if content_type in _VECTOR:
        try:
            blob = _to_png(blob, _VECTOR[content_type])
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            return Reading(error=f"could not convert {content_type}: {error}")
    try:
        picture = Image.open(io.BytesIO(blob))
        picture.load()
    except (UnidentifiedImageError, OSError) as error:
        return Reading(error=f"could not open picture: {error}")

    if picture.mode in ("RGBA", "LA", "P"):  # flatten transparency onto white paper
        picture = picture.convert("RGBA")
        paper = Image.new("RGBA", picture.size, "white")
        picture = Image.alpha_composite(paper, picture)
    gray = picture.convert("L")
    if min(gray.size) < _MIN_SIDE:
        return Reading(size=gray.size)

    words = read_pixels(gray)
    weight = sum(len(w.text) for w in words)
    conf = sum(w.conf * len(w.text) for w in words) / weight if words else None
    return Reading(layout.build_lines(words), conf, gray.size)


def _to_png(blob: bytes, suffix: str) -> bytes:
    command = os.environ.get("SOFFICE_CMD") or shutil.which("soffice")
    if not command and _WINDOWS_SOFFICE.exists():
        command = str(_WINDOWS_SOFFICE)
    if not command:
        raise RuntimeError("LibreOffice not found (set SOFFICE_CMD)")
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / f"picture{suffix}"
        source.write_bytes(blob)
        # A private profile, so a LibreOffice window the user has open does not block us.
        profile = (Path(folder) / "profile").as_uri()
        subprocess.run(
            [command, "--headless", f"-env:UserInstallation={profile}",
             "--convert-to", "png", "--outdir", folder, str(source)],
            check=True, capture_output=True, timeout=180,
        )
        result = source.with_suffix(".png")
        if not result.exists():
            raise RuntimeError("LibreOffice produced no output")
        return result.read_bytes()


def emit(doc: Document, region: Block, reading: Reading, place=None) -> None:
    """Attach what was read to an image region: one child block per line of text.

    place maps a box in the picture's pixels to a box on the page, when the
    picture's position on the page is known in page units (PDF). Without it,
    word boxes stay in the picture's own pixels, which is what masking the
    picture itself needs (DOCX, PPTX).
    """
    def box(x0, y0, x1, y1):
        return place(x0, y0, x1, y1) if place else BBox(x0, y0, x1, y1, Unit.PX)

    if reading.error:
        region.quarantined = True
        region.native_ref = {**(region.native_ref or {}), "unreadable": reading.error}
        return
    if reading.size:
        region.native_ref = {**(region.native_ref or {}), "pixels": list(reading.size)}
    if reading.conf is None:
        return
    region.source = Source.OCR
    region.ocr_conf = reading.conf
    region.quarantined = reading.conf < MIN_IMAGE_CONFIDENCE
    for line in reading.lines:
        text, spans = layout.compose(line.words)
        doc.add(
            BlockType.PARAGRAPH,
            text,
            container=Container.IMAGE,
            source=Source.OCR,
            parent_id=region.block_id,
            page=region.page,
            section_path=region.section_path,
            quarantined=region.quarantined,
            ocr_conf=sum(w.conf for w in line.words) / len(line.words),
            bbox=box(line.x0, line.y0, line.x1, line.y1),
            words=[Word(a, b, box(w.x0, w.y0, w.x1, w.y1), w.conf) for a, b, w in spans],
        )
