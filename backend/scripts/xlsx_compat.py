"""Workaround for real compatibility issues in the actual OKW workbook —
none of which touch sheet data, formulas, or cached values, so this only
patches container-level XML plumbing before handing off to openpyxl:

1. The workbook is **OOXML Strict** conformance (`conformance="strict"`,
   `http://purl.oclc.org/ooxml/...` namespaces) rather than the far more
   common Transitional conformance (`http://schemas.openxmlformats.org/...`)
   that openpyxl is built around. Under Strict namespaces, openpyxl can't
   resolve `r:id` sheet references at all and silently loads zero sheets
   (no exception — this is the nastier of the two bugs here). Fixed by
   rewriting the two Strict namespace URIs to their Transitional equivalents
   everywhere in the package.
2. Stale `externalReferences` (leftover links to other workbooks copy-pasted
   from at some point) that openpyxl's dataclass validation can't handle:
   `TypeError: ExternalReference.__init__() missing 1 required positional
   argument: 'id'`.
3. A handful of font `family` values (34) outside the spec's valid 0-14
   range, which fail stylesheet parsing entirely.

Isolated here, not inlined into the parser or loader, so it's obvious this
is a defensive workaround and not part of the actual data model.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path


def load_workbook_safely(path: str | Path, **kwargs):
    """`openpyxl.load_workbook`, tolerant of broken externalReferences.

    Tries a normal load first; only falls back to the stripped-copy path if
    that raises, so well-formed workbooks pay zero extra cost.
    """
    from openpyxl import load_workbook

    try:
        return load_workbook(path, **kwargs)
    except (TypeError, ValueError) as exc:
        message = str(exc)
        needs_fix = "ExternalReference" in message or "could not read stylesheet" in message
        if not needs_fix:
            raise
        cleaned = _sanitize_workbook(path)
        try:
            return load_workbook(cleaned, **kwargs)
        finally:
            Path(cleaned).unlink(missing_ok=True)


# OOXML Strict -> Transitional namespace rewrites (see module docstring
# point 1). Applied globally across every XML part in the package: the
# `r:` relationships namespace appears in workbook.xml, every worksheet, and
# every `.rels` file; the main spreadsheetml namespace appears in workbook.xml
# and every worksheet.
_STRICT_TO_TRANSITIONAL_NAMESPACES = {
    b"http://purl.oclc.org/ooxml/officeDocument/relationships": (
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ),
    b"http://purl.oclc.org/ooxml/spreadsheetml/main": (
        b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ),
}

_MAX_VALID_FONT_FAMILY = 14


def _clamp_invalid_font_families(styles_xml: bytes) -> bytes:
    """Some font entries in the real workbook have `family` values above the
    spec's valid 0-14 range (observed: 34) — presumably leftover cruft from
    years of copy/paste in Excel. `0` ("not applicable") is always valid and
    doesn't change how the cell displays in any way that matters here, since
    we only read values, never render the workbook.
    """

    def _clamp(match: re.Match) -> bytes:
        value = int(match.group(1))
        return b'<family val="0"/>' if value > _MAX_VALID_FONT_FAMILY else match.group(0)

    return re.sub(rb'<family val="(\d+)"/>', _clamp, styles_xml)


def _sanitize_workbook(path: str | Path) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(
        tmp_path, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/externalLinks/"):
                continue  # drop the external-link parts entirely
            if item.filename == "xl/workbook.xml":
                data = re.sub(
                    rb"<externalReferences>.*?</externalReferences>",
                    b"",
                    data,
                    flags=re.S,
                )
            elif item.filename == "xl/_rels/workbook.xml.rels":
                data = re.sub(
                    rb'<Relationship[^>]*Target="externalLinks/[^"]*"[^>]*/>',
                    b"",
                    data,
                )
            elif item.filename == "[Content_Types].xml":
                data = re.sub(
                    rb'<Override[^>]*PartName="/xl/externalLinks/[^"]*"[^>]*/>',
                    b"",
                    data,
                )
            if b"<family val=" in data:
                # Not just styles.xml — comment sheets embed their own font
                # definitions too, with the same out-of-range values.
                data = _clamp_invalid_font_families(data)
            if item.filename.endswith(".xml") or item.filename.endswith(".rels"):
                for old_ns, new_ns in _STRICT_TO_TRANSITIONAL_NAMESPACES.items():
                    data = data.replace(old_ns, new_ns)
                data = data.replace(b' conformance="strict"', b"")
            dst.writestr(item, data)

    return tmp_path
