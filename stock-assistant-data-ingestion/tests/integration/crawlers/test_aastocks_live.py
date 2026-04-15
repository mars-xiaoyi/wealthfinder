"""Live integration test for AAStocksCrawler."""

import pytest

from app.crawl.crawlers.aastocks_crawler import AAStocksCrawler

from .conftest import assert_success_shape, summarize


@pytest.mark.live
@pytest.mark.asyncio
async def test_aastocks_live_end_to_end(source_config, page_crawler, stub_db):
    crawler = AAStocksCrawler(
        source_config=source_config,
        page_crawler=page_crawler,
        db=stub_db,
    )

    result = await crawler.run()
    summarize("AASTOCKS", result)

    assert len(result.successes) >= 1, "no AAStocks articles crawled"
    assert_success_shape(result.successes[0], host_contains="aastocks.com")
