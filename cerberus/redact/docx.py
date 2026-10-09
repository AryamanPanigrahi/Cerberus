"""Masked copy of a DOCX: same document, tokens where the PII was.

A .docx is a zip of XML parts, and personal data can sit in more of them than
the page shows. The masked copy deals with each:

- Text: each finding is replaced by its token inside the run that holds it,
  in the body, tables, text boxes, headers, footers and notes.
- Pictures: a picture two OCR engines read and agreed on is rebuilt from its
  pixels with the PII, anything shaped like PII, faces and barcodes
  painted over. Any other picture is replaced by a grey placeholder. Either
  way the original image is no longer referenced, so it is not written to
  the new file.
- Second copies: a text box is stored twice, once for current Word and once
  for Word 2007 and older. Only the current copy is scanned, so after it has
  been masked its contents are copied over the old one: both stay in the
  file, and they cannot disagree. An old copy that cannot be matched up, or
  that holds text the writer does not control, is removed instead. A picture's
  SVG twin is removed.
- Tracked changes: deleted text is removed for good, and the names of the
  people who made changes or comments are blanked.
- File properties: a property naming a person holds that person's token, or
  nothing if no name was detected in it.
- Link targets that contain PII are neutralised, and the document variables
  in the settings part are removed.

Before a paragraph is changed, its text is rebuilt and compared with what
detection saw. A mismatch stops the run: a token written at the wrong offset
would leave the real value in place.
"""

from __future__ import annotations

import copy
import io
import re
from pathlib import Path

import docx
from docx.oxml import parse_xml
from lxml import etree

from ..extract.ir import BlockType, Document
from . import office
from .tokens import Tokenized

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_V = "urn:schemas-microsoft-com:vml"


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


_SKIP = {_w("pPr"), _w("rPr"), _w("instrText"), _w("drawing"), _w("pict"), _w("object")}
_FIXED = {_w("tab"): "\t", _w("br"): "\n", _w("cr"): "\n", _w("noBreakHyphen"): "-"}


def mask_docx(
    source: str | Path, doc: Document, tokenized: list[Tokenized], out: str | Path, pictures: str = "findings"
) -> Path:
    """Write a masked copy of `source` to `out`. `doc` is the extraction of the same file."""
    spans = office.spans_by_block(doc, tokenized)
    src = docx.Document(str(source))
    parts = {str(p.partname): p for p in src.part.package.iter_parts()}

    # The root of each part, as the extractor saw it. Parts the library has no
    # class for are parsed here and written back at the end.
    roots, parsed = {}, {}

    def root_of(name: str):
        if name not in roots:
            part = parts[name]
            if name == str(src.part.partname):
                roots[name] = src.element.body
            elif getattr(part, "element", None) is not None:
                roots[name] = part.element
            else:
                roots[name] = parsed[name] = parse_xml(part.blob)
        return roots[name]

    # Paragraphs are addressed by their position in the part, so they are
    # listed before anything is removed from the tree.
    paragraphs = {}
    for block in doc.blocks:
        name = (block.native_ref or {}).get("part")
        if name in parts and name not in paragraphs and "p" in block.native_ref:
            paragraphs[name] = list(root_of(name).iter(_w("p")))

    for block in doc.blocks:
        found = spans.get(block.block_id)
        ref = block.native_ref or {}
        if not found or "p" not in ref or block.type is BlockType.METADATA:
            continue
        holders, pieces = [], []
        for index, number in enumerate(ref["p"]):
            if index:
                holders.append(None)
                pieces.append("\n")
            for holder, text in _pieces(paragraphs[ref["part"]][number]):
                holders.append(holder)
                pieces.append(text)
        if "".join(pieces) != block.text:
            raise ValueError(f"{doc.name}: paragraph text no longer matches what was scanned; not written")
        fixed = frozenset(i for i, holder in enumerate(holders) if holder is None)
        for holder, new, old in zip(holders, office.apply_spans(pieces, found, fixed), pieces):
            if new != old:
                holder.text = new
                holder.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    sizes = office.pictures_by_key(
        doc, office.picture_boxes(doc, tokenized, pictures), lambda ref: (ref.get("part"), ref.get("rid"))
    )
    flagged_links = {b.text for b in doc.blocks if b.type is BlockType.METADATA and spans.get(b.block_id)}
    for name, part in parts.items():
        if not re.fullmatch(r"/word/[^/]+\.xml", name) or name in ("/word/styles.xml", "/word/numbering.xml"):
            continue
        try:
            root = root_of(name)
        except Exception:  # not XML we can read: nothing of ours is in it
            continue
        top = root.getparent() if root.getparent() is not None else root
        _remove_duplicates_and_history(top)
        _mask_pictures(part, top, name, sizes)
        for rel in part.rels.values():
            if rel.is_external and rel.target_ref in flagged_links:
                rel._target = "about:blank"  # the address itself was PII
        for variables in top.iter(_w("docVars")):
            variables.getparent().remove(variables)

    _rewrite_properties(src, doc, spans, parts)
    for name, root in parsed.items():
        parts[name]._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    src.save(str(out))
    return out


def _pieces(paragraph):
    """(element or None, text) for each piece of a paragraph's visible text, in
    the order and by the rules the extractor used. None marks a tab or break."""
    def visit(element):
        for child in element:
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP or tag in (_w("del"), _w("moveFrom")):
                continue
            if tag == _w("t"):
                yield child, child.text or ""
            elif tag in _FIXED:
                yield None, _FIXED[tag]
            elif tag == f"{{{_MC}}}AlternateContent":
                choice = child.find(f"{{{_MC}}}Choice")
                yield from visit(choice if choice is not None else child)
            else:
                yield from visit(child)

    yield from visit(paragraph)


def _remove_duplicates_and_history(root) -> None:
    _sync_old_word_copies(root)
    doomed = [e for e in root.iter(_w("del"), _w("moveFrom"))]
    for element in doomed:
        if element.getparent() is not None:
            element.getparent().remove(element)
    for element in root.iter():
        for attribute in (_w("author"), _w("initials")):
            if isinstance(element.tag, str) and element.get(attribute):
                element.set(attribute, "")


def _sync_old_word_copies(root) -> None:
    """Make the old-Word copy of each text box say exactly what the masked one says.

    Word writes a drawing twice: a current version (Choice) and one for Word
    2007 and older (Fallback). Tokens were written into the current version.
    Each text box in the old version is emptied and refilled with a copy of
    its masked counterpart, so there is one piece of masking logic, not two.

    If the two versions do not hold the same number of text boxes they cannot
    be paired, and old WordArt keeps its text in an attribute this writer does
    not rewrite. In either case the old version is removed: losing it costs a
    reader on Word 2007 a text box; keeping it could cost a leak.
    """
    for wrapper in list(root.iter(f"{{{_MC}}}AlternateContent")):
        current, old = wrapper.find(f"{{{_MC}}}Choice"), wrapper.find(f"{{{_MC}}}Fallback")
        if current is None or old is None:
            continue
        masked, stale = list(current.iter(_w("txbxContent"))), list(old.iter(_w("txbxContent")))
        if len(masked) != len(stale) or next(old.iter(f"{{{_V}}}textpath"), None) is not None:
            wrapper.remove(old)
            continue
        for source, target in zip(masked, stale):
            for child in list(target):
                target.remove(child)
            for child in source:
                target.append(copy.deepcopy(child))


def _mask_pictures(part, root, name: str, sizes: dict) -> None:
    old = set()
    for blip in list(root.iter(f"{{{_A}}}blip", f"{{{_V}}}imagedata")):
        attribute = f"{{{_R}}}embed" if blip.get(f"{{{_R}}}embed") else f"{{{_R}}}id"
        rid = blip.get(attribute)
        rel = part.rels.get(rid) if rid else None
        if rel is None or rel.is_external:
            continue  # linked, not embedded: nothing in the file to replace
        if hasattr(part, "get_or_add_image"):
            boxes, size = sizes.get((name, rid), (None, None))  # a picture extraction did not see is masked whole
            original = rel.target_part
            masked = office.covered_picture(original.blob, original.content_type, boxes, size)
            new, _ = part.get_or_add_image(io.BytesIO(masked))
            blip.set(attribute, new)
            for extra in blip.findall(f"{{{_A}}}extLst"):
                blip.remove(extra)  # an SVG twin of the picture would be referenced from here
        else:
            # No way to attach a replacement to this kind of part: take the picture out.
            graphic = next((a for a in blip.iterancestors() if a.tag in (_w("drawing"), _w("pict"))), blip)
            graphic.getparent().remove(graphic)
        old.add(rid)
    for described in root.iter(f"{{{_WP}}}docPr"):
        described.attrib.pop("descr", None)
    # A relationship nothing points at any more is dropped, and its image with it.
    still_used = {v for e in root.iter() if isinstance(e.tag, str) for k, v in e.attrib.items() if k.startswith(f"{{{_R}}}")}
    for rid in old - still_used:
        part.rels.pop(rid, None)


def _rewrite_properties(src, doc: Document, spans: dict, parts: dict) -> None:
    properties = office.core_property_blocks(doc)
    for name in ("author", "last_modified_by", "title", "subject", "keywords", "comments", "category"):
        if name in properties:
            setattr(src.core_properties, name, office.property_value(properties[name], name, spans))
        elif name in office.PERSON_PROPERTIES:
            setattr(src.core_properties, name, "")

    # Application and cover-page properties live in other parts, as plain XML.
    for block in doc.blocks:
        ref = block.native_ref or {}
        field = ref.get("app_property") or ref.get("cover_property")
        if block.type is not BlockType.METADATA or not field or not spans.get(block.block_id):
            continue
        wanted = "/docProps/app.xml" if "app_property" in ref else None
        for name, part in parts.items():
            if (wanted and name != wanted) or (not wanted and not re.fullmatch(r"/customXml/item\d+\.xml", name)):
                continue
            root = parse_xml(part.blob)
            changed = False
            for leaf in root.iter():
                if isinstance(leaf.tag, str) and leaf.tag.rsplit("}", 1)[-1] == field and (leaf.text or "").strip() == block.text:
                    leaf.text = office.property_value(block, field, spans)
                    changed = True
            if changed:
                part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
