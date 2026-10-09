import pytest


@pytest.fixture(autouse=True)
def one_ocr_engine(request, monkeypatch):
    """Tests read pictures with Tesseract alone, so they give the same result
    whether or not the neural engine is installed. A test marked `neural` gets both."""
    if "neural" not in request.keywords:
        monkeypatch.setenv("CERBERUS_OCR", "tesseract")
