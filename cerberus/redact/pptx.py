"""Masked copy of a PPTX: same slides and layout, tokens where the PII was.

- Text: each finding is replaced by its token inside the run that holds it,
  so fonts, colours and positions are untouched.
- Pictures: replaced by a grey placeholder of the same shape. The original
  image is no longer referenced, so it is not written to the new file.
- File properties: a property naming a person holds that person's token, or
  nothing if no name was detected in it.
- Picture alt text is cleared and speaker notes are tokenised like slide text.

Before a text frame is changed, its text is rebuilt and compared with what
detection saw. A mismatch stops the run: a token written at the wrong offset
would leave the real value in place.
"""

from __future__ import annotations

import io
from pathlib import Path

import pptx
from pptx.oxml.ns import qn
from pptx.shapes.group import GroupShape
from pptx.shapes.picture import Picture

from ..extract.ir import BlockType, Document
from . import office
from .tokens import Tokenized


def mask_pptx(source: str | Path, doc: Document, tokenized: list[Tokenized], out: str | Path) -> Path:
    """Write a masked copy of `source` to `out`. `doc` is the extraction of the same file."""
    spans = office.spans_by_block(doc, tokenized)
    prs = pptx.Presentation(str(source))
    slides = list(prs.slides)
    sizes = {
        (b.native_ref or {}).get("sha1"): tuple((b.native_ref or {}).get("pixels") or ()) or None
        for b in doc.blocks if b.type is BlockType.IMAGE_REGION
    }

    for block in doc.blocks:
        found = spans.get(block.block_id)
        if not found or block.page is None:
            continue
        slide = slides[block.page - 1]
        ref = block.native_ref or {}
        if block.type is BlockType.NOTES:
            frame = slide.notes_slide.notes_text_frame
        elif block.type is BlockType.METADATA:
            continue  # a link target or alt text: handled below
        else:
            tree = {"slide": slide, "layout": slide.slide_layout, "master": slide.slide_layout.slide_master}
            shape = _find(tree[ref.get("part", "slide")].shapes, ref.get("shape_id"))
            if shape is None:
                raise ValueError(f"{doc.name}: shape {ref.get('shape_id')} on slide {block.page} not found")
            frame = (
                shape.table.cell(block.row, block.col).text_frame
                if block.type is BlockType.TABLE_CELL else shape.text_frame
            )
        _rewrite(frame, block.text, found, f"{doc.name} slide {block.page}")

    flagged_links = {b.text for b in doc.blocks if b.type is BlockType.METADATA and spans.get(b.block_id)}
    replaced: dict = {}  # part -> relationship ids that pointed at an original picture
    for slide in slides:
        layout = slide.slide_layout
        for tree in (slide, layout, layout.slide_master):
            for shape in _all(tree.shapes):
                if isinstance(shape, Picture):
                    old = _mask_picture(shape, sizes)
                    if old:
                        replaced.setdefault(shape.part, set()).add(old)
                elif shape.has_text_frame:
                    for run in (r for p in shape.text_frame.paragraphs for r in p.runs):
                        if run.hyperlink.address in flagged_links:
                            run.hyperlink.address = None  # the address itself was PII

    # A relationship nothing points at any more is dropped, and its image with it.
    relationship = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    for part, old in replaced.items():
        used = {v for e in part._element.iter() if isinstance(e.tag, str)
                for k, v in e.attrib.items() if k.startswith(relationship)}
        for rid in old - used:
            part.rels.pop(rid)

    properties = office.core_property_blocks(doc)
    for name in ("author", "last_modified_by", "title", "subject", "keywords", "comments", "category"):
        if name in properties:
            setattr(prs.core_properties, name, office.property_value(properties[name], name, spans))
        elif name in office.PERSON_PROPERTIES:
            setattr(prs.core_properties, name, "")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out


def _all(shapes):
    for shape in shapes:
        yield shape
        if isinstance(shape, GroupShape):
            yield from _all(shape.shapes)


def _find(shapes, shape_id):
    return next((s for s in _all(shapes) if s.shape_id == shape_id), None)


def _rewrite(frame, expected: str, spans, where: str) -> None:
    """Put tokens into a text frame, run by run."""
    # The pieces of each paragraph, as the extractor joined them: runs and
    # fields carry text; a line break counts as one character.
    holders, pieces = [], []
    for index, paragraph in enumerate(frame.paragraphs):
        if index:
            holders.append(None)
            pieces.append("\n")
        for child in paragraph._p.content_children:
            text = child.find(qn("a:t"))
            holders.append(text)
            pieces.append((text.text or "") if text is not None else "\n")
    if "".join(pieces) != expected:
        raise ValueError(f"{where}: text no longer matches what was scanned; not written")
    for holder, new, old in zip(holders, office.apply_spans(pieces, spans), pieces):
        if new != old:
            if holder is None:
                raise ValueError(f"{where}: a finding runs across a line break")
            holder.text = new


def _mask_picture(shape: Picture, sizes: dict) -> str | None:
    """Point the picture at a placeholder. Returns the relationship id it used before."""
    blip = shape._element.blipFill.blip
    try:
        key = shape.image.sha1
    except ValueError:  # linked, not embedded: nothing in the file to replace
        return None
    old = blip.rEmbed
    _, rid = shape.part.get_or_add_image_part(io.BytesIO(office.placeholder(sizes.get(key))))
    blip.rEmbed = rid
    for extra in blip.findall(qn("a:extLst")):
        blip.remove(extra)  # an SVG twin of the picture would be referenced from here
    for described in shape._element.xpath("./p:nvPicPr/p:cNvPr"):
        described.attrib.pop("descr", None)
    return old if old != rid else None
