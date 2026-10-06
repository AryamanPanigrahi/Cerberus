"""Find framed figures on a page image.

On a scanned page a figure is not a separate object: it is pixels like the
rest. What marks it out is its frame, a rectangle of thin lines. Text lines,
table rules and heading underlines all produce long horizontal lines too, but
only a frame has vertical edges joining a top line to a bottom line.

Words inside a frame are unreliable at page resolution (small print,
screenshots), so the caller cuts each frame out and OCRs it again, enlarged.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageFilter, ImageOps

# Fractions of the page width / height.
_MIN_WIDTH = 0.2
_MIN_HEIGHT = 0.04
_INK = 232  # grey level below which a pixel counts as drawn (white paper is ~255)
_TARGET_WIDTH = 3600  # enlarge a figure to about this many pixels across before OCR
_MAX_SCALE = 6


def find_frames(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Outermost frames in a greyscale page, as (x0, y0, x1, y1) pixel boxes, top to bottom."""
    height, width = gray.shape
    ink = gray < _INK
    lines = _horizontal_lines(ink, int(_MIN_WIDTH * width))

    frames = []
    for i, (top, x0, x1) in enumerate(lines):
        for bottom, bx0, bx1 in lines[i + 1 :]:
            if bottom - top < _MIN_HEIGHT * height:
                continue
            tolerance = 0.01 * width
            if abs(bx0 - x0) > tolerance or abs(bx1 - x1) > tolerance:
                continue
            if _is_edge(ink, x0, top, bottom) and _is_edge(ink, x1, top, bottom):
                frames.append((x0, top, x1, bottom))
                break  # the nearest matching bottom line closes this frame

    def inside(a, b):
        return a != b and a[0] >= b[0] - 3 and a[1] >= b[1] - 3 and a[2] <= b[2] + 3 and a[3] <= b[3] + 3

    outer = [f for f in frames if not any(inside(f, other) for other in frames)]
    return sorted(set(outer), key=lambda f: f[1])


def _horizontal_lines(ink: np.ndarray, min_length: int) -> list[tuple[int, int, int]]:
    """Long unbroken horizontal runs of ink, as (y, x0, x1), one per run of adjacent rows."""
    found: list[tuple[int, int, int]] = []
    last_row = -10
    for y in np.flatnonzero(ink.sum(axis=1) >= min_length):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], ink[y].view(np.int8), [0]))))
        starts, ends = edges[::2], edges[1::2]
        longest = int(np.argmax(ends - starts))
        x0, x1 = int(starts[longest]), int(ends[longest]) - 1
        if x1 - x0 + 1 < min_length:
            continue
        # A thick line or a filled band covers many adjacent rows: count it once.
        same = found and y - last_row <= 1 and abs(found[-1][1] - x0) <= 3 and abs(found[-1][2] - x1) <= 3
        last_row = int(y)
        if not same:
            found.append((int(y), x0, x1))
    return found


def _is_edge(ink: np.ndarray, x: int, top: int, bottom: int) -> bool:
    """Is there a vertical line at about x, running from top to bottom?"""
    strip = ink[top:bottom, max(0, x - 3) : x + 4]
    return bool(strip.any(axis=1).mean() >= 0.9)


def prepare(figure: Image.Image) -> tuple[Image.Image, int, int, int]:
    """Make a cut-out figure as readable as possible for OCR.

    Returns the prepared image and how to map its pixels back: the enlargement
    factor and the (x, y) offset of the crop inside the original cut-out.

    - Crop to the content, dropping the frame and its white padding.
    - Enlarge with a smooth (Lanczos) filter. Small figures are enlarged more,
      because their print is smaller. Enlarging adds no information, but OCR
      engines are tuned for letters a few dozen pixels tall.
    - Wherever the surroundings are dark (a terminal, a sidebar), invert, so
      text is dark on light everywhere. OCR reads light-on-dark poorly.
    """
    border = 6
    ink = np.asarray(figure)[border:-border, border:-border] < _INK
    rows, columns = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    if len(rows) == 0:
        return figure, 1, 0, 0
    left, top = int(columns[0]) + border, int(rows[0]) + border
    content = figure.crop((left, top, int(columns[-1]) + border + 1, int(rows[-1]) + border + 1))

    scale = min(_MAX_SCALE, max(2, math.ceil(_TARGET_WIDTH / content.width)))
    image = content.resize((content.width * scale, content.height * scale), Image.LANCZOS)

    coarse = image.resize((max(1, image.width // 16), max(1, image.height // 16)), Image.BOX)
    background = coarse.filter(ImageFilter.MedianFilter(5)).resize(image.size, Image.BILINEAR)
    dark = background.point(lambda level: 255 if level < 128 else 0)
    image = Image.composite(ImageOps.invert(image), image, dark)
    return ImageOps.autocontrast(image, cutoff=1), scale, left, top
