from enum import Enum


class CrawlSourceName(str, Enum):
    """
    All known source identifiers. Owned by the crawler domain; imported by the
    API layer (``app/api/schemas.py``) for request validation. Adding a new
    source requires:

    1. Adding a new member here.
    2. Implementing the corresponding Crawler class in ``app/crawl/``.
    3. Registering it in ``CrawlService.CRAWLER_REGISTRY``.
    """

    HKEX = "HKEX"
    MINGPAO = "MINGPAO"
    AASTOCKS = "AASTOCKS"
    YAHOO_HK = "YAHOO_HK"
