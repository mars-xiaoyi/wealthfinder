import hashlib
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


def normalise(text: str) -> str:
    """
    Apply NFKC normalisation and whitespace cleaning to a string.

    Converts fullwidth characters to halfwidth, strips leading/trailing whitespace,
    and collapses internal sequences of whitespace/newlines to a single space.

    If normalisation raises an unexpected exception, log a warning and return the
    original text unchanged.

    Pure text utility — no domain coupling. Used by the crawl layer (to compute
    ``raw_hash`` on raw_news inserts) and the cleaning layer (to produce the
    cleaned title/body).
    """
    try:
        normalised = unicodedata.normalize("NFKC", text)
        normalised = re.sub(r"\s+", " ", normalised)
        return normalised.strip()
    except Exception:
        logger.warning(
            "[text_utils] normalise failed for input (len=%d); returning original",
            len(text) if isinstance(text, str) else -1,
            exc_info=True,
        )
        return text


def compute_hash(text: str) -> str:
    """
    Compute the SHA-256 hex digest of a string.

    Pure hash utility — callers pass whatever they want hashed (typically an
    already-normalised title). Returns a 64-character lowercase hex string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
