from pathlib import Path

from .ir import Document


def extract(path: str | Path) -> Document:
    """Extract any supported file into the block IR, choosing the extractor by extension."""
    suffix = Path(path).suffix.lower()
    # Imported here so that reading one format does not need the others' libraries.
    if suffix == ".pdf":
        from .pdf import extract_pdf as run
    elif suffix == ".docx":
        from .docx import extract_docx as run
    elif suffix == ".pptx":
        from .pptx import extract_pptx as run
    else:
        raise ValueError(f"unsupported file type: {suffix or path}")
    return run(path)
