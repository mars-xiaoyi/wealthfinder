"""
Live integration test for HKEXCrawler.

WARNING: a full run fetches every PDF HKEX published on the target date
(typically several hundred), so this can take multiple minutes. Only runs
with `pytest -m live`.
"""

from datetime import date

import pytest

from app.crawl.crawlers.hkex_crawler import HKEXCrawler

from .conftest import assert_success_shape, summarize


@pytest.mark.live
@pytest.mark.asyncio
async def test_hkex_live_end_to_end(source_config, page_crawler, stub_db):
    crawler = HKEXCrawler(
        source_config=source_config,
        page_crawler=page_crawler,
        db=stub_db,
        crawl_date=date(2026, 4, 14),
    )

    result = await crawler.run()
    summarize("HKEX", result)

    assert len(result.successes) >= 1, "no HKEX filings crawled"
    first = result.successes[0]
    assert_success_shape(first, host_contains="hkexnews.hk")
    assert first.source_url.endswith(".pdf")
    assert first.extra_metadata and first.extra_metadata.get("stock_code"), (
        "HKEX successes must carry stock_code metadata"
    )
