import fitz
import pytest

from app.crawl.parsers.pdf_parser import PdfEncryptedException, PdfParseException, parse_pdf


def _make_pdf(text: str = "Hello PDF world") -> bytes:
    """Create a simple single-page PDF with the given text."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    content = doc.tobytes()
    doc.close()
    return content


def _make_encrypted_pdf() -> bytes:
    """Create a password-protected PDF."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Secret content")
    content = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pass")
    doc.close()
    return content


def _make_empty_pdf() -> bytes:
    """Create a PDF with no text content."""
    doc = fitz.open()
    doc.new_page()
    content = doc.tobytes()
    doc.close()
    return content


# ---------------------------------------------------------------------------
# parse_pdf — happy path (primary parser succeeds)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_pdf_primary_success():
    content = _make_pdf("Extracted PDF text")
    result = await parse_pdf(content)
    assert "Extracted PDF text" in result


# ---------------------------------------------------------------------------
# parse_pdf — encrypted PDF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_pdf_encrypted_raises():
    content = _make_encrypted_pdf()
    with pytest.raises(PdfEncryptedException):
        await parse_pdf(content)


# ---------------------------------------------------------------------------
# parse_pdf — empty PDF (both parsers return empty)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_pdf_empty_raises():
    content = _make_empty_pdf()
    with pytest.raises(PdfParseException, match="Both primary and fallback"):
        await parse_pdf(content)


# ---------------------------------------------------------------------------
# parse_pdf — invalid bytes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_pdf_invalid_bytes_raises():
    with pytest.raises(Exception):
        await parse_pdf(b"not a pdf")
