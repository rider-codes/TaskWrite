"""PDF, DOCX, and TXT extraction. Extracted text is untrusted data."""

from __future__ import annotations

import io
import re
from pathlib import Path

MAX_PDF_PAGES = 200
MAX_TEXT_CHARS = 200_000
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}


class ExtractionError(Exception):
    """Raised when a file cannot be read as a supported assignment."""


def extract_text(filename: str, data: bytes, max_bytes: int = 10 * 1024 * 1024) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ExtractionError(f"Unsupported file type '{ext or 'none'}'. Use PDF, DOCX, or TXT.")
    if not data:
        raise ExtractionError("The uploaded file is empty.")
    if len(data) > max_bytes:
        raise ExtractionError(f"File is larger than the {max_bytes // (1024 * 1024)} MB limit.")
    if ext == ".pdf":
        text = _extract_pdf(data)
    elif ext == ".docx":
        text = _extract_docx(data)
    else:
        text = _extract_txt(data)
    text = _normalize(text)
    if not text.strip():
        raise ExtractionError(
            "No text could be extracted. Scanned or image-only files need OCR, which is not supported."
        )
    return text[:MAX_TEXT_CHARS]


def _normalize(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_txt(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _extract_pdf(data: bytes) -> str:
    if not data.startswith(b"%PDF"):
        raise ExtractionError("File does not look like a valid PDF.")
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ExtractionError("Password-protected PDFs are not supported.")
            pages = [page.get_text() for page in doc.pages(0, min(doc.page_count, MAX_PDF_PAGES))]
        return "\n\n".join(pages)
    except ExtractionError:
        raise
    except Exception as exc:  # pymupdf raises various error types
        raise ExtractionError(f"Could not read the PDF: {type(exc).__name__}") from exc


def _extract_docx(data: bytes) -> str:
    if not data.startswith(b"PK"):
        raise ExtractionError("File does not look like a valid DOCX.")
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(io.BytesIO(data))
        parts: list[str] = []
        for child in document.element.body.iterchildren():
            if child.tag.endswith("}p"):
                parts.append(Paragraph(child, document).text)
            elif child.tag.endswith("}tbl"):
                for row in Table(child, document).rows:
                    parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    except Exception as exc:
        raise ExtractionError(f"Could not read the DOCX: {type(exc).__name__}") from exc
