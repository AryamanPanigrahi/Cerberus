"""A second OCR engine for pictures: docTR, a neural network, run on the CPU.

Tesseract was built for clean printed pages. The pictures in these documents
are something else: screenshots, a terminal with coloured text on black, a
scanned form with print a few pixels tall. A neural engine trained on scene
text and documents reads many of those where Tesseract returns nothing.

docTR works in two steps: one network finds where the words are, a second
reads each word it was shown. Two details decide whether that works here:

- The first network looks at the whole picture shrunk to 1024 pixels. Small
  print in a wide picture is then a pixel or two tall and is not found at all.
  So the picture is enlarged until its text is a comfortable height, and cut
  into overlapping tiles of 1024 pixels, each read at full size.
- Enlarging is capped so that one picture is at most a handful of tiles: the
  time taken grows with the number of tiles, and a screenshot enlarged four
  times would take a minute on its own.
- A word lying across the edge of a tile is seen in two pieces. The tiles
  overlap by more than a long word, so each word is whole in at least one
  tile; where two tiles report the same place, the wider reading is kept.

The model weights live in the gitignored `models/doctr` folder and are loaded
from there. Nothing is fetched while the pipeline runs: download them once
with  python -m cerberus.extract.neural --download
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from . import figures
from .ocr import OcrWord

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "doctr"
DETECTOR = "db_resnet50"  # finds the words
RECOGNISER = "crnn_vgg16_bn"  # reads them
_TEXT_HEIGHT = 28  # pixels; the word height the networks handle best
_MAX_SCALE = 4
_TILE = 1024  # the size the first network works at
_OVERLAP = 384  # wider than a long e-mail address at _TEXT_HEIGHT
_MAX_TILES = 8  # per picture; each tile is about a second and a half on a CPU


def available() -> bool:
    """Are the library and both sets of weights on this machine?"""
    try:
        import doctr  # noqa: F401
    except ImportError:
        return False
    stored = MODEL_DIR / "models"
    return all(any(stored.glob(f"{name}-*.pt")) for name in (DETECTOR, RECOGNISER))


def engine_version() -> str:
    import doctr

    return f"{doctr.__version__} {DETECTOR}+{RECOGNISER}"


@lru_cache(maxsize=1)
def _predictor(download: bool = False):
    if not download and not available():
        raise RuntimeError(
            "docTR or its weights are missing. Install python-doctr, then run "
            "`python -m cerberus.extract.neural --download` once."
        )
    os.environ["DOCTR_CACHE_DIR"] = str(MODEL_DIR)  # read by docTR when it looks for its weights
    from doctr.models import ocr_predictor

    return ocr_predictor(det_arch=DETECTOR, reco_arch=RECOGNISER, pretrained=True)


def tiles(width: int, height: int, size: int = _TILE, overlap: int = _OVERLAP) -> list[tuple[int, int, int, int]]:
    """Overlapping squares that cover a picture, as (x0, y0, x1, y1)."""
    def starts(length: int) -> list[int]:
        if length <= size:
            return [0]
        stride = size - overlap
        steps = list(range(0, length - size, stride))
        return steps + [length - size]  # the last tile is pulled back to end at the edge

    return [(x, y, min(width, x + size), min(height, y + size)) for y in starts(height) for x in starts(width)]


def merge(found: list[tuple[OcrWord, tuple[int, int, int, int]]], size: tuple[int, int]) -> list[OcrWord]:
    """One list of words from the readings of overlapping tiles.

    found pairs each word (in whole-picture pixels) with the tile it came from.
    A word touching an inner edge of its tile may be cut short there. It is
    dropped if another tile read the same place; if no other tile did, a
    possibly cut word is still better than none.
    """
    width, height = size

    def cut(word: OcrWord, tile) -> bool:
        x0, y0, x1, y1 = tile
        return (
            (word.x0 - x0 < 3 and x0 > 0) or (x1 - word.x1 < 3 and x1 < width)
            or (word.y0 - y0 < 3 and y0 > 0) or (y1 - word.y1 < 3 and y1 < height)
        )

    def same_place(a: OcrWord, b: OcrWord) -> bool:
        across = min(a.x1, b.x1) - max(a.x0, b.x0)
        down = min(a.y1, b.y1) - max(a.y0, b.y0)
        if across <= 0 or down <= 0:
            return False
        smaller = min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0))
        return across * down > 0.5 * smaller

    # Whole words first, widest first: the first word to claim a place keeps it.
    ranked = sorted(found, key=lambda pair: (cut(*pair), -(pair[0].x1 - pair[0].x0), -pair[0].conf))
    kept: list[OcrWord] = []
    for word, _ in ranked:
        if not any(same_place(word, other) for other in kept):
            kept.append(word)
    return _number_lines(kept)


def _number_lines(words: list[OcrWord]) -> list[OcrWord]:
    """Give words on the same baseline the same line number, top to bottom."""
    out: list[OcrWord] = []
    line, bottom, top = -1, None, None
    for word in sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0)):
        middle = (word.y0 + word.y1) / 2
        if top is None or not (top <= middle <= bottom):
            line, top, bottom = line + 1, word.y0, word.y1
        out.append(OcrWord(word.text, word.x0, word.y0, word.x1, word.y1, word.conf, (0, 0, line)))
    return out


def read_pixels(gray: Image.Image, border: int = 0, text_height: float | None = None, predictor=None) -> list[OcrWord]:
    """The words in a greyscale picture, positioned in that picture's pixels.

    text_height is how tall the picture's text is, if a first pass measured
    it; the picture is enlarged to bring it to the height the networks like.
    Without it, small pictures are enlarged three times and large ones twice.
    """
    predictor = predictor or _predictor()
    if text_height:
        wanted = round(_TEXT_HEIGHT / text_height)
    else:
        wanted = 3 if max(gray.size) < 1200 else 2
    wanted = max(1, min(_MAX_SCALE, wanted))
    while True:
        image, scale, left, top = figures.prepare(gray, border, scale=wanted)
        boxes = tiles(*image.size)
        if len(boxes) <= _MAX_TILES or wanted == 1:
            break
        wanted -= 1  # a large picture: read it a little smaller instead of for minutes
    colour = image.convert("RGB")
    result = predictor([np.asarray(colour.crop(box)) for box in boxes])

    found = []
    for box, page in zip(boxes, result.pages):
        tile_width, tile_height = box[2] - box[0], box[3] - box[1]
        for block in page.blocks:
            for line in block.lines:
                for word in line.words:
                    if not word.value.strip():
                        continue
                    (x0, y0), (x1, y1) = word.geometry[:2]
                    found.append((OcrWord(
                        word.value, box[0] + x0 * tile_width, box[1] + y0 * tile_height,
                        box[0] + x1 * tile_width, box[1] + y1 * tile_height, float(word.confidence), (0, 0, 0),
                    ), box))
    return [
        OcrWord(w.text, left + w.x0 / scale, top + w.y0 / scale, left + w.x1 / scale, top + w.y1 / scale, w.conf, w.line)
        for w in merge(found, image.size)
    ]


if __name__ == "__main__":
    import sys

    if "--download" in sys.argv:
        _predictor(download=True)
        print("docTR weights are in", MODEL_DIR)
    else:
        print("available" if available() else "not available: run with --download")
