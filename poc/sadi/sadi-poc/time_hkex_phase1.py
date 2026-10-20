"""
Time HKEX Phase 1 (playwright pagination) to pick a defensible default for
CRAWL_BROWSER_NAV_TIMEOUT_MS.

The SADI HKEXCrawler applies the same timeout to every individual playwright
op (page.goto, page.click, page.wait_for_load_state). This script measures
each op independently so we can base the default on the slowest *individual*
op, not the total run duration.

Usage (from repo root):
    stock-assistant-data-ingestion/.venv/bin/python poc/sadi/time_hkex_phase1.py
    stock-assistant-data-ingestion/.venv/bin/python poc/sadi/time_hkex_phase1.py --date 2026-04-14 --runs 3
"""

import argparse
import asyncio
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from playwright.async_api import async_playwright

HKEX_SEARCH_URL_TEMPLATE = (
    "https://www1.hkexnews.hk/search/titlesearch.xhtml"
    "?lang=EN&category=0&market=SEHK&searchType=0&stockId="
    "&from={date}&to={date}&title="
)

PAGINATION_SELECTOR = ".component-loadmore-leftPart__container"
LOAD_MORE_SELECTOR = "a.component-loadmore__link"
ROW_SELECTOR = "table tbody tr"

MAX_LOAD_MORE_CLICKS = 30
GENEROUS_TIMEOUT_MS = 60_000


@dataclass
class RunStats:
    run_idx: int
    target_date: str
    goto_ms: float = 0.0
    click_ms: list[float] = field(default_factory=list)
    wait_after_click_ms: list[float] = field(default_factory=list)
    query_rows_ms: float = 0.0
    total_clicks: int = 0
    row_count: int = 0
    total_ms: float = 0.0


def _parse_pagination_counts(text: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\D+(\d+)", text)
    if match is None:
        raise ValueError(f"Cannot parse pagination text: {text!r}")
    return int(match.group(1)), int(match.group(2))


async def time_one_run(run_idx: int, target_date: date) -> RunStats:
    stats = RunStats(run_idx=run_idx, target_date=target_date.isoformat())
    url = HKEX_SEARCH_URL_TEMPLATE.format(date=target_date.strftime("%Y%m%d"))
    print(f"\n=== Run {run_idx} — {target_date.isoformat()} ===")
    print(f"[run {run_idx}] URL: {url}")

    run_start = time.perf_counter()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        t0 = time.perf_counter()
        await page.goto(url, wait_until="domcontentloaded", timeout=GENEROUS_TIMEOUT_MS)
        stats.goto_ms = (time.perf_counter() - t0) * 1000
        print(f"[run {run_idx}] page.goto: {stats.goto_ms:.0f} ms")

        for click_idx in range(MAX_LOAD_MORE_CLICKS):
            pagination_text = await page.text_content(PAGINATION_SELECTOR) or ""
            try:
                showing, total = _parse_pagination_counts(pagination_text)
            except ValueError:
                print(f"[run {run_idx}] Pagination text unparseable: {pagination_text!r}; stopping")
                break
            if showing >= total:
                print(f"[run {run_idx}] All {total} rows visible after {click_idx} click(s)")
                break

            t_click = time.perf_counter()
            await page.click(LOAD_MORE_SELECTOR, timeout=GENEROUS_TIMEOUT_MS)
            click_dur = (time.perf_counter() - t_click) * 1000
            stats.click_ms.append(click_dur)

            # Wait until the "showing" counter actually advances. LOAD MORE fires
            # an XHR that is invisible to domcontentloaded / load events; polling
            # the pagination text is the only reliable signal.
            t_wait = time.perf_counter()
            await page.wait_for_function(
                """
                ({selector, prev}) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    const m = (el.textContent || '').match(/(\\d+)\\D+(\\d+)/);
                    if (!m) return false;
                    return parseInt(m[1], 10) > prev;
                }
                """,
                arg={"selector": PAGINATION_SELECTOR, "prev": showing},
                timeout=GENEROUS_TIMEOUT_MS,
            )
            wait_dur = (time.perf_counter() - t_wait) * 1000
            stats.wait_after_click_ms.append(wait_dur)

            stats.total_clicks = click_idx + 1
            print(
                f"[run {run_idx}] click #{click_idx + 1}: click={click_dur:.0f} ms, "
                f"wait={wait_dur:.0f} ms (was {showing}/{total})"
            )
        else:
            print(f"[run {run_idx}] Hit MAX_LOAD_MORE_CLICKS={MAX_LOAD_MORE_CLICKS}")

        t_rows = time.perf_counter()
        rows = await page.query_selector_all(ROW_SELECTOR)
        stats.query_rows_ms = (time.perf_counter() - t_rows) * 1000
        stats.row_count = len(rows)
        print(
            f"[run {run_idx}] query_selector_all: {stats.query_rows_ms:.0f} ms "
            f"({stats.row_count} rows)"
        )

        await context.close()
        await browser.close()

    stats.total_ms = (time.perf_counter() - run_start) * 1000
    print(f"[run {run_idx}] total run: {stats.total_ms:.0f} ms")
    return stats


def _summarize(label: str, samples: list[float]) -> None:
    if not samples:
        print(f"  {label}: (no samples)")
        return
    samples_sorted = sorted(samples)
    p95 = samples_sorted[min(len(samples_sorted) - 1, int(0.95 * len(samples_sorted)))]
    print(
        f"  {label}: n={len(samples)} "
        f"min={min(samples):.0f} "
        f"median={statistics.median(samples):.0f} "
        f"p95={p95:.0f} "
        f"max={max(samples):.0f} ms"
    )


def summarize(all_runs: list[RunStats]) -> None:
    print("\n=== Summary across all runs ===")
    goto_samples = [r.goto_ms for r in all_runs]
    query_samples = [r.query_rows_ms for r in all_runs]
    click_samples = [c for r in all_runs for c in r.click_ms]
    wait_samples = [w for r in all_runs for w in r.wait_after_click_ms]
    per_op = click_samples + wait_samples + goto_samples
    total_samples = [r.total_ms for r in all_runs]

    _summarize("page.goto", goto_samples)
    _summarize("page.click(LOAD_MORE)", click_samples)
    _summarize("wait_for_load_state", wait_samples)
    _summarize("query_selector_all", query_samples)
    _summarize("ALL per-op (goto + click + wait)", per_op)
    _summarize("total run (wallclock)", total_samples)

    for r in all_runs:
        print(
            f"  run {r.run_idx}: clicks={r.total_clicks} rows={r.row_count} "
            f"total={r.total_ms:.0f} ms"
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-04-14", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    all_runs: list[RunStats] = []
    for i in range(1, args.runs + 1):
        stats = await time_one_run(i, target_date)
        all_runs.append(stats)

    summarize(all_runs)


if __name__ == "__main__":
    asyncio.run(main())
