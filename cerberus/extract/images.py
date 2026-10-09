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

Two engines read each picture when a second one is installed: Tesseract, and
RapidOCR (rapid.py) or, failing that, docTR (neural.py). CERBERUS_OCR chooses:
"tesseract" for one engine, "rapidocr" or "doctr" to name the second. They
fail in different ways, so what either one reads
is kept, each as its own lines, and the detectors see both. Having two
readings also gives a way to know how far to trust them:

- a word both engines read the same way, in the same place, is confirmed;
- a word only one of them read, or that they read differently, is not. It may
  be a name one engine got wrong. It is listed to be covered in the masked
  copy, whether or not a detector recognised it;
- if the engines agree on too little of a picture, the whole picture is
  quarantined, as an unreadable one is.

Faces and barcodes (regions.py) are listed to be covered too.
"""

from __future__ import annotations

import hashlib
import io
import os
import pickle
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from PIL import Image, UnidentifiedImageError

from . import figures, layout, neural, ocr, rapid, regions
from .ir import BBox, Block, BlockType, Container, Document, Source, Unit, Word

MIN_IMAGE_CONFIDENCE = 0.60  # starting value, shared with figures on scanned pages
MIN_AGREEMENT = 0.50  # share of a picture's text the two engines must read alike for it to be trusted
_MIN_WORD = 3  # letters or digits; shorter scraps (icon noise, bullets) are not weighed
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
    lines: list[layout.Line] = field(default_factory=list)  # Tesseract's words, in the picture's own pixels
    conf: float | None = None  # None: no text found
    size: tuple[int, int] | None = None
    error: str = ""  # set when the picture could not be read at all
    second: list[layout.Line] | None = None  # the second engine's words; None if it did not run
    engine: str = ""  # which second engine read it; "" in readings cached before there was a choice (docTR)
    agreement: float | None = None  # share of the text both engines read alike; None if only one ran or no text
    cover: list[tuple[str, tuple[float, float, float, float]]] = field(default_factory=list)  # (kind, box) to mask

    @property
    def trusted(self) -> bool:
        """Can this picture be masked word by word, or must it be masked whole?"""
        if self.error or self.second is None:
            return False  # one reader cannot check itself
        if self.conf is None:
            return True  # both engines found no text at all
        return self.conf >= MIN_IMAGE_CONFIDENCE and (self.agreement or 0.0) >= MIN_AGREEMENT


_cache: Path | None = None


def use_cache(folder: str | Path | None) -> None:
    """Keep what was read from each picture in this folder and reuse it next time.

    Reading a picture with two engines takes seconds; the same pixels always
    give the same reading. The files hold text read from the documents, so the
    folder must be one that is never committed (the pipeline uses its output folder).
    """
    global _cache
    _cache = Path(folder) if folder else None


def second_engine() -> str | None:
    """Which second engine reads pictures, if any: "rapidocr", "doctr" or None.

    CERBERUS_OCR=tesseract switches the second engine off (fast mode, and for
    comparison); "rapidocr" or "doctr" asks for that one. Otherwise the best
    one installed is used: RapidOCR read more of the labelled figures than
    docTR, in less time, so it comes first.
    """
    wanted = os.environ.get("CERBERUS_OCR", "both").lower()
    if wanted == "tesseract":
        return None
    order = [wanted] if wanted in ("rapidocr", "doctr") else ["rapidocr", "doctr"]
    for name in order:
        if (rapid if name == "rapidocr" else neural).available():
            return name
    return None


def use_neural() -> bool:
    """Is a second engine in use?"""
    return second_engine() is not None


def best_reading(image: Image.Image) -> list[ocr.OcrWord]:
    """OCR under several segmentation modes and keep the best reading.

    No single mode suits every picture: a form reads best as one block (6), a
    badge or card as a column (4), a diagram as scattered text (11). The best
    reading is the one with the most confidently read characters.
    """
    def score(words):
        return sum(w.conf * len(w.text) for w in words if w.conf >= 0.6)

    return max((ocr.ocr_image(image, psm=mode) for mode in (6, 4, 11)), key=score)


def text_height(gray: Image.Image, border: int = 0) -> float | None:
    """How tall the picture's text is, in its own pixels, from a quick first reading."""
    probe = figures.prepare(gray, border, scale=1)[0]
    heights = [w.height for w in ocr.ocr_image(probe, psm=11) if w.conf >= 0.6 and len(w.text) > 2]
    return median(heights) if heights else None


def read_pixels(gray: Image.Image, border: int = 0, height: float | None = -1) -> list[ocr.OcrWord]:
    """The words in a greyscale picture, positioned in that picture's pixels.

    The picture is read twice. A first quick pass measures how tall its text
    is; the picture is then enlarged (or left alone) so that text comes out at
    the height OCR reads best, and read properly. If the first pass finds
    nothing it can trust, the text is probably tiny, and the enlargement is
    guessed from the picture's width instead. Pass `height` if already measured.
    """
    if height == -1:
        height = text_height(gray, border)
    wanted = round(_TEXT_HEIGHT / height) if height else None
    image, scale, left, top = figures.prepare(gray, border, scale=wanted)
    return [
        ocr.OcrWord(
            w.text, left + w.x0 / scale, top + w.y0 / scale,
            left + w.x1 / scale, top + w.y1 / scale, w.conf, w.line,
        )
        for w in best_reading(image)
    ]


def _letters(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _inside(inner: ocr.OcrWord, outer: ocr.OcrWord) -> float:
    """Share of inner's box that lies within outer's."""
    across = min(inner.x1, outer.x1) - max(inner.x0, outer.x0)
    down = min(inner.y1, outer.y1) - max(inner.y0, outer.y0)
    area = (inner.x1 - inner.x0) * (inner.y1 - inner.y0)
    return across * down / area if across > 0 and down > 0 and area > 0 else 0.0


def compare(first: list[ocr.OcrWord], second: list[ocr.OcrWord]) -> tuple[float | None, list[ocr.OcrWord]]:
    """How far two readings of one picture agree, and the words they do not agree on.

    The engines cut text into words differently (one reads an e-mail address
    as a single word, the other as three), so a word is compared with
    everything the other engine read in the same place, joined together.
    Only letters and digits are compared: a stray comma is not a disagreement.
    """
    def confirmed(words, others) -> list[bool]:
        out = []
        for word in words:
            there = sorted((o for o in others if _inside(o, word) > 0.5), key=lambda o: o.x0)
            out.append(bool(there) and _letters("".join(o.text for o in there)) == _letters(word.text))
        return out

    ok_first, ok_second = confirmed(first, second), confirmed(second, first)
    # A piece of a confirmed word is confirmed with it.
    for words, flags, others, other_flags in ((first, ok_first, second, ok_second), (second, ok_second, first, ok_first)):
        whole = [o for o, good in zip(others, list(other_flags)) if good]
        for index, word in enumerate(words):
            if not flags[index] and any(_inside(word, o) > 0.5 for o in whole):
                flags[index] = True

    total = agreed = 0
    doubtful = []
    for word, good in [*zip(first, ok_first), *zip(second, ok_second)]:
        size = len(_letters(word.text))
        if size < _MIN_WORD:
            continue
        total += size
        agreed += size if good else 0
        if not good:
            doubtful.append(word)
    return (agreed / total if total else None), doubtful


def examine(gray: Image.Image, border: int = 0, colour: Image.Image | None = None) -> Reading:
    """Read a picture with every engine available and work out what to cover.

    Positions are in the picture's own pixels. colour is the same picture in
    colour, if there is one: face detection works better on it.
    """
    stored = None
    if _cache is not None:
        name = second_engine()
        version = {"rapidocr": rapid.engine_version, "doctr": neural.engine_version}[name]() if name else ""
        engines = f"tesseract+{version}" if name else "tesseract"
        key = hashlib.sha1(gray.tobytes() + f"{gray.size}{border}{engines}".encode()).hexdigest()
        stored = _cache / f"{key}.pkl"
        if stored.exists():
            return pickle.loads(stored.read_bytes())
    reading = _examine(gray, border, colour)
    if stored is not None:
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(pickle.dumps(reading))
    return reading


def _examine(gray: Image.Image, border: int, colour: Image.Image | None) -> Reading:
    height = text_height(gray, border)
    words = read_pixels(gray, border, height)
    weight = sum(len(w.text) for w in words)
    reading = Reading(layout.build_lines(words), sum(w.conf * len(w.text) for w in words) / weight if words else None, gray.size)
    name = second_engine()
    if name is None:
        return reading

    if name == "rapidocr":  # reads the picture as it is, in colour if there is colour
        other = rapid.read_pixels(colour if colour is not None and colour.size == gray.size else gray, border)
    else:
        other = neural.read_pixels(gray, border, text_height=height)
    reading.second = layout.build_lines(other)
    reading.engine = name
    reading.agreement, doubtful = compare(words, other)
    if other:
        weight = sum(len(w.text) for w in other)
        reading.conf = max(reading.conf or 0.0, sum(w.conf * len(w.text) for w in other) / weight)
    reading.cover = [("unconfirmed", (w.x0, w.y0, w.x1, w.y1)) for w in doubtful]
    reading.cover += regions.find(colour or gray)
    return reading


class Reader:
    """Reads pictures for one document, remembering those already seen."""

    def __init__(self):
        self._seen: dict[str, Reading] = {}

    def read(self, blob: bytes, content_type: str | None = None) -> Reading:
        key = hashlib.sha1(blob).hexdigest()
        if key not in self._seen:
            self._seen[key] = _read(blob, content_type)
        return self._seen[key]


def open_picture(blob: bytes, content_type: str | None = None) -> Image.Image:
    """The picture as pixels, on white paper. Raises ValueError if it cannot be opened or converted."""
    if content_type in _VECTOR:
        try:
            blob = _to_png(blob, _VECTOR[content_type])
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise ValueError(f"could not convert {content_type}: {error}") from error
    try:
        picture = Image.open(io.BytesIO(blob))
        picture.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(f"could not open picture: {error}") from error
    if picture.mode in ("RGBA", "LA", "P"):  # flatten transparency onto white paper
        picture = picture.convert("RGBA")
        paper = Image.new("RGBA", picture.size, "white")
        picture = Image.alpha_composite(paper, picture)
    return picture.convert("RGB")


def _read(blob: bytes, content_type: str | None) -> Reading:
    try:
        picture = open_picture(blob, content_type)
    except ValueError as error:
        return Reading(error=str(error))
    gray = picture.convert("L")
    if min(gray.size) < _MIN_SIDE:
        return Reading(size=gray.size)
    return examine(gray, colour=picture)


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
    describe(region, reading, box)
    if reading.conf is None:
        return
    region.source = Source.OCR
    region.ocr_conf = reading.conf
    for engine, lines in (("tesseract", reading.lines), (reading.engine or "doctr", reading.second or [])):
        for line in lines:
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
                native_ref={"engine": engine},
            )


def describe(region: Block, reading: Reading, box) -> None:
    """Record on an image region how it was read and how it must be masked.

    native_ref gains: engines (which ran), agreement, trusted (may be masked
    word by word) and cover (rectangles to mask whatever the detectors say:
    words the engines disagree on, faces, barcodes), in the region's own units.
    """
    quarantined = reading.conf is not None and reading.conf < MIN_IMAGE_CONFIDENCE
    if reading.second is not None and reading.conf is not None:
        quarantined = quarantined or (reading.agreement or 0.0) < MIN_AGREEMENT
    region.quarantined = region.quarantined or quarantined
    cover = []
    for kind, (x0, y0, x1, y1) in reading.cover:
        placed = box(x0, y0, x1, y1)
        cover.append([kind, placed.x0, placed.y0, placed.x1, placed.y1])
    region.native_ref = {
        **(region.native_ref or {}),
        "engines": ["tesseract"] + ([reading.engine or "doctr"] if reading.second is not None else []),
        "agreement": None if reading.agreement is None else round(reading.agreement, 3),
        "trusted": reading.trusted,
        "cover": cover,
    }
