"""OCR engine wrapper: an image in, positioned words out.

Everything downstream sees only OcrWord, so the engine can be swapped (for
PaddleOCR or docTR) without touching layout analysis or the IR. Confidence is
normalised to 0..1 for the same reason.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytesseract
from PIL import Image

_WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


@dataclass(frozen=True)
class OcrWord:
    text: str
    x0: float  # pixels in the image that was OCR'd
    y0: float
    x1: float
    y1: float
    conf: float  # 0..1
    line: tuple[int, int, int]  # the engine's own line grouping (block, paragraph, line)

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@lru_cache(maxsize=1)
def _configure() -> str:
    """Locate the Tesseract binary once; fail with instructions if it is missing."""
    command = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not command and _WINDOWS_DEFAULT.exists():
        command = str(_WINDOWS_DEFAULT)
    if not command:
        raise RuntimeError(
            "Tesseract not found. Install it, or set TESSERACT_CMD to the tesseract executable."
        )
    pytesseract.pytesseract.tesseract_cmd = command
    return str(pytesseract.get_tesseract_version())


def engine_version() -> str:
    return _configure()


def ocr_image(image: Image.Image, psm: int = 4) -> list[OcrWord]:
    """Recognise the words in an image.

    psm is Tesseract's page segmentation mode. 4 ("one column of text of varying
    sizes") keeps numbered lists and table rows on their own lines; the fully
    automatic mode 3 splits list numbers into a separate column and loses them.
    """
    _configure()
    data = pytesseract.image_to_data(
        image, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
    )
    words = []
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        left, top = data["left"][i], data["top"][i]
        words.append(
            OcrWord(
                text=text,
                x0=left,
                y0=top,
                x1=left + data["width"][i],
                y1=top + data["height"][i],
                conf=max(0.0, float(data["conf"][i])) / 100,
                line=(data["block_num"][i], data["par_num"][i], data["line_num"][i]),
            )
        )
    return words
