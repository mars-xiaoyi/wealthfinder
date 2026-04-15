import logging
from typing import Optional

import trafilatura
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def extract_body_auto(html: str) -> Optional[str]:
    """
    Extract the main article body from HTML using trafilatura auto-extraction.
    Returns extracted plain text, or None if trafilatura returns empty/None.
    """
    result = trafilatura.extract(html)
    if not result or not result.strip():
        logger.debug("[html_parser] trafilatura returned empty for input (%d chars)", len(html))
        return None
    text = result.strip()
    logger.debug("[html_parser] trafilatura extracted %d chars", len(text))
    return text


def extract_body_css(html: str, selector: str) -> Optional[str]:
    """
    Extract text from HTML using a CSS selector (BS4 fallback).
    Use when trafilatura quality is insufficient or a specific element is needed.

    Returns extracted plain text from the matched element, or None if selector doesn't match.
    """
    soup = BeautifulSoup(html, "html.parser")
    element = soup.select_one(selector)
    if element is None:
        logger.debug("[html_parser] CSS selector '%s' matched nothing", selector)
        return None
    text = element.get_text(separator=" ", strip=True)
    if not text:
        logger.debug("[html_parser] CSS selector '%s' matched empty element", selector)
        return None
    logger.debug("[html_parser] CSS selector '%s' extracted %d chars", selector, len(text))
    return text
