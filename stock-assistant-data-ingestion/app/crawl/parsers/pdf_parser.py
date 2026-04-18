import asyncio
import logging
from io import BytesIO

import fitz  # pymupdf
from pdfminer.high_level import extract_text as pdfminer_extract_text

logger = logging.getLogger(__name__)


class PdfEncryptedException(Exception):
    """Raised when the PDF is encrypted. Non-retryable — log and discard."""


class PdfParseException(Exception):
    """Raised when both parsers fail on a non-encrypted PDF."""


def _parse_primary(content: bytes) -> str:
    """Primary PDF text extraction. Returns extracted text (may be empty)."""
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        if doc.is_encrypted:
            raise PdfEncryptedException("PDF is encrypted")
        text_parts: list[str] = []
        for page in doc:
            text_parts.append(page.get_text())
        return "".join(text_parts).strip()
    finally:
        doc.close()


def _parse_fallback(content: bytes) -> str:
    """Fallback PDF text extraction. Returns extracted text (may be empty)."""
    return pdfminer_extract_text(BytesIO(content)).strip()


async def parse_pdf(content: bytes) -> str:
    """
    Extract plain text from a PDF binary. Tries primary parser first; falls back
    to secondary parser if primary returns empty text (complex layout PDFs).

    Raises:
        PdfEncryptedException: if PDF is encrypted (non-retryable)
        PdfParseException: if both parsers fail on a non-encrypted PDF
    """
    logger.debug("[pdf_parser] Parsing PDF (%d bytes) with primary parser", len(content))
    text = await asyncio.to_thread(_parse_primary, content)
    if text:
        logger.debug("[pdf_parser] Primary parser extracted %d chars", len(text))
        return text

    logger.info("[pdf_parser] Primary parser returned empty text, falling back to secondary parser")
    try:
        text = await asyncio.to_thread(_parse_fallback, content)
    except Exception as exc:
        raise PdfParseException(f"Fallback parser failed: {exc}") from exc

    if not text:
        raise PdfParseException("Both primary and fallback parsers returned empty text")

    logger.debug("[pdf_parser] Fallback parser extracted %d chars", len(text))
    return text
