"""Parts of a picture that are personal data without being text: faces and barcodes.

OCR finds words. A photograph of a face, or a barcode that encodes a staff
number, holds no words and would pass straight through word-level masking.
These are found from the pixels and masked as whole rectangles.

- Faces: OpenCV's YuNet detector, a small neural network. Its weights are a
  local file in the gitignored `models/opencv` folder; without it faces are
  not looked for, and `available()` says so.
- Barcodes and QR codes: pyzbar and OpenCV's own detector, which find codes
  they can decode. A printed code is often too small or too damaged to decode,
  so a third check looks for the pattern itself: a block of vertical bars that
  is the same from its top row to its bottom row.

A drawn silhouette or an avatar is not a face and is not reported as one.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

FACE_MODEL = Path(__file__).resolve().parents[2] / "models" / "opencv" / "face_detection_yunet_2023mar.onnx"
_FACE_SCORE = 0.6
_MIN_BARS = 12  # dark bars across a barcode; text has far fewer in a row that match top to bottom

Box = tuple[float, float, float, float]


def available() -> dict[str, bool]:
    """Which detectors can run on this machine."""
    found = {"faces": False, "barcodes": False}
    try:
        import cv2

        found["faces"] = FACE_MODEL.exists() and hasattr(cv2, "FaceDetectorYN")
        found["barcodes"] = True
    except ImportError:
        pass
    return found


def find(picture: Image.Image) -> list[tuple[str, Box]]:
    """("face" | "barcode", box in the picture's pixels) for each region found."""
    try:
        import cv2
    except ImportError:
        return []
    colour = np.ascontiguousarray(np.asarray(picture.convert("RGB"))[:, :, ::-1])  # OpenCV wants blue first
    gray = np.asarray(picture.convert("L"))
    found = [("face", box) for box in _faces(cv2, colour)]
    codes = _decoded(cv2, colour, gray) + _bars(cv2, gray)
    kept: list[Box] = []
    for box in sorted(codes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        if not any(_overlap(box, other) > 0.5 for other in kept):
            kept.append(box)
    return found + [("barcode", box) for box in kept]


def _overlap(a: Box, b: Box) -> float:
    """Share of the smaller box that lies inside the other."""
    across, down = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if across <= 0 or down <= 0:
        return 0.0
    return across * down / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))


@lru_cache(maxsize=1)
def _face_detector():
    import cv2

    return cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (320, 320), _FACE_SCORE)


def _faces(cv2, colour: np.ndarray) -> list[Box]:
    if not available()["faces"]:
        return []
    height, width = colour.shape[:2]
    boxes = []
    # A small face in a large picture is missed at full size and found when the
    # picture is shrunk, and the other way round: look at two sizes.
    for target in (640, 1280):
        k = min(1.0, target / max(width, height)) if target == 640 else min(2.0, target / max(width, height))
        shown = cv2.resize(colour, (max(1, round(width * k)), max(1, round(height * k))))
        detector = _face_detector()
        detector.setInputSize((shown.shape[1], shown.shape[0]))
        _, faces = detector.detect(shown)
        for face in faces if faces is not None else []:
            x, y, w, h = (float(v) / k for v in face[:4])
            pad = 0.25 * max(w, h)  # hair, ears and chin lie outside the detector's box
            box = (max(0, x - pad), max(0, y - pad), min(width, x + w + pad), min(height, y + h + pad))
            if not any(_overlap(box, other) > 0.5 for other in boxes):
                boxes.append(box)
    return boxes


def _decoded(cv2, colour: np.ndarray, gray: np.ndarray) -> list[Box]:
    boxes: list[Box] = []
    try:
        from pyzbar import pyzbar

        for code in pyzbar.decode(Image.fromarray(gray)):
            r = code.rect
            boxes.append((r.left, r.top, r.left + r.width, r.top + r.height))
    except Exception:  # the zbar library is missing or refused the picture: the pattern check still runs
        pass
    for detector in (getattr(cv2, "QRCodeDetector", None), getattr(getattr(cv2, "barcode", None), "BarcodeDetector", None)):
        if detector is None:
            continue
        try:
            result = detector().detectMulti(colour)
        except Exception:
            continue
        corners = result[1] if isinstance(result, tuple) and result[0] else None
        for points in corners if corners is not None else []:
            points = np.asarray(points).reshape(-1, 2)
            boxes.append((float(points[:, 0].min()), float(points[:, 1].min()),
                          float(points[:, 0].max()), float(points[:, 1].max())))
    return boxes


def _bars(cv2, gray: np.ndarray) -> list[Box]:
    """Blocks of vertical bars, found by their pattern without decoding them."""
    height, width = gray.shape
    across = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    down = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    # Bars change fast from left to right and not at all from top to bottom.
    edges = cv2.convertScaleAbs(cv2.subtract(np.abs(across), np.abs(down)))
    edges = cv2.blur(edges, (7, 3))
    _, solid = cv2.threshold(edges, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    solid = cv2.morphologyEx(solid, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3)))
    solid = cv2.erode(solid, None, iterations=2)
    solid = cv2.dilate(solid, None, iterations=2)
    contours, _ = cv2.findContours(solid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 40 or h < 8 or w < 1.5 * h or w * h < 0.002 * width * height:
            continue
        patch = gray[y : y + h, x : x + w]
        ink = patch < (int(patch.min()) + int(patch.max())) / 2
        middle = ink[int(h * 0.2) : max(int(h * 0.8), int(h * 0.2) + 1)]
        filled = middle.mean(axis=0)  # per column: how much of it is dark
        # In a barcode every column is a bar or a gap, dark or light all the way down.
        # Text never is: its columns are dark in some rows and light in others.
        pure = float(np.mean((filled < 0.1) | (filled > 0.9)))
        dark = filled > 0.9
        bars = int(np.count_nonzero(np.diff(dark.astype(np.int8)) == 1))
        if bars >= _MIN_BARS and pure >= 0.9 and 0.2 <= dark.mean() <= 0.8:
            boxes.append((max(0.0, x - 2.0), max(0.0, y - 2.0), min(width, x + w + 2.0), min(height, y + h + 2.0)))
    return boxes
