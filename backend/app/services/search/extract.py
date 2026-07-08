"""Best-effort plain-text extraction from rich Drive files.

Google-native docs are exported by the Drive API; this covers the binary
formats Drive can't export — PDFs and uploaded OOXML Office files (docx / pptx /
xlsx) — by pulling their text out of the downloaded bytes locally. Returns None
for anything unsupported (images, legacy .doc/.xls, unknown binaries) so the
caller can fall back to just linking the file.
"""

from __future__ import annotations

import io
import zipfile

from lxml import etree

# OOXML mimetypes → all are zip archives of XML parts.
_OOXML_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
}


def extract_text(mime: str, data: bytes, *, max_chars: int) -> str | None:
    try:
        if mime == "application/pdf":
            text = _pdf(data)
        elif mime in _OOXML_MIMES:
            text = _ooxml(data)
        else:
            return None
    except Exception:  # noqa: BLE001 — extraction is best-effort; fall back to a link
        return None
    text = " ".join((text or "").split()).strip()
    return text[:max_chars] if text else None


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _ooxml(data: bytes) -> str:
    """Pull text nodes from the readable parts of an OOXML archive: docx body
    (`w:t`), pptx slides (`a:t`), xlsx shared strings (`t`). Namespace-agnostic —
    match on the local tag name `t`, which is the text node in all three."""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        targets = [
            name
            for name in zf.namelist()
            if name == "word/document.xml"
            or (name.startswith("ppt/slides/slide") and name.endswith(".xml"))
            or name == "xl/sharedStrings.xml"
        ]
        for name in targets:
            root = etree.fromstring(zf.read(name))
            for el in root.iter():
                if etree.QName(el).localname == "t" and el.text:
                    parts.append(el.text)
    return " ".join(parts)
