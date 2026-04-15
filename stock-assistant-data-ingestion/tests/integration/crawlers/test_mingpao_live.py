"""Live integration test for MingPaoCrawler."""

import pytest

from app.crawl.crawlers.mingpao_crawler import MingPaoCrawler

from .conftest import assert_success_shape, summarize


@pytest.mark.live
@pytest.mark.asyncio
async def test_mingpao_live_end_to_end(source_config, page_crawler, stub_db):
    crawler = MingPaoCrawler(
        source_config=source_config,
        page_crawler=page_crawler,
        db=stub_db,
    )

    result = await crawler.run()
    summarize("MINGPAO", result)

    assert len(result.successes) >= 1, "no MingPao articles crawled"
    assert_success_shape(result.successes[0], host_contains="mingpao.com")
