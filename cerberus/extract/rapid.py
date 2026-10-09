"""A second OCR engine for pictures: RapidOCR (the PP-OCR models, run with ONNX Runtime on the CPU).

The PP-OCR models are trained on small and on-screen text: screenshots,
terminals, forms photographed or scanned at low resolution. That is what the
pictures inside these documents are. Measured on the labelled figures (see
CLAUDE.md), it read more of them than Tesseract and docTR together, in a
fraction of the time, and it needs no help: the picture is given as it is,
not enlarged and not cut into tiles.

Like every engine here it is hidden behind OcrWord, so nothing downstream
knows which engine read a word.

The models are files inside the installed package. Nothing is fetched while
the pipeline runs, and `available()` checks they are there.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from .ocr import OcrWord


def _models() -> list[Path]:
    import rapidocr

    return sorted((Path(rapidocr.__file__).parent / "models").glob("*.onnx"))


def available() -> bool:
    """Is the library installed, with a detection and a recognition model beside it?"""
    try:
        names = [path.name for path in _models()]
    except ImportError:
        return False
    return any("_det" in name for name in names) and any("_rec" in name for name in names)


def engine_version() -> str:
    import rapidocr

    models = "+".join(path.stem for path in _models() if "_det" in path.name or "_rec" in path.name)
    return f"rapidocr {getattr(rapidocr, '__version__', '?')} {models}"


@lru_cache(maxsize=1)
def _engine():
    if not available():
        raise RuntimeError("RapidOCR or its models are missing: pip install rapidocr onnxruntime")
    import logging

    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    from rapidocr import RapidOCR

    return RapidOCR()


def read_pixels(picture: Image.Image, border: int = 0) -> list[OcrWord]:
    """The words in a picture, positioned in that picture's pixels.

    border pixels are cut off each edge first (the frame drawn round a figure
    on a scanned page would otherwise be read as strokes of text).
    """
    width, height = picture.size
    if border and min(width, height) > 4 * border:
        picture = picture.crop((border, border, width - border, height - border))
    else:
        border = 0
    result = _engine()(np.asarray(picture.convert("RGB")), return_word_box=True)
    words: list[OcrWord] = []

    def add(text, score, corners, line: int) -> None:
        if str(text).strip():
            xs, ys = [point[0] for point in corners], [point[1] for point in corners]
            words.append(OcrWord(
                str(text), border + min(xs), border + min(ys), border + max(xs), border + max(ys),
                float(score), (0, 0, line),
            ))

    texts = list(result.txts or ())
    by_word = list(result.word_results or ())
    for line, text in enumerate(texts):
        pieces = [piece for piece in (by_word[line] if line < len(by_word) else ()) if len(piece) == 3]
        if pieces:
            for piece in pieces:
                add(*piece, line)
        else:  # the engine gave no word positions for this line: keep the line as one word, not nothing
            add(text, result.scores[line], result.boxes[line], line)
    return words
