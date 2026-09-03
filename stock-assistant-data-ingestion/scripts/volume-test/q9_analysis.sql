-- Q-9 data collection query — run at the end of the test window (or
-- periodically) against pg_stat_statements. Surfaces the exact 3 queries in
-- CleaningService.process_record's success path and the 2 in its rejection
-- path (see app/cleaner/cleaning_service.py) with real call counts and
-- latency, no application code changes required.
--
-- Read this together with the goal-2 daily-volume query in
-- volume-test-plan.md: multiply mean_ms below by real observed daily volume
-- to estimate total DB time/day attributable to CleaningService, and judge
-- whether that's large enough relative to the 24h crawl-to-crawl gap to be
-- worth batching.

SELECT
    calls,
    round(mean_exec_time::numeric, 3)   AS mean_ms,
    round(stddev_exec_time::numeric, 3) AS stddev_ms,
    round(total_exec_time::numeric, 3)  AS total_ms,
    left(query, 90)                     AS query_shape
FROM pg_stat_statements
WHERE query ILIKE '%raw_news%' OR query ILIKE '%cleaned_news%'
ORDER BY total_ms DESC;

-- Sanity cross-check: `calls` for the raw_news fetch query should roughly
-- match raw_ingested (from the goal-2 query) summed across the window —
-- confirms pg_stat_statements is actually capturing CleaningService's
-- traffic and not being reset mid-run (e.g. by a container restart, which
-- clears pg_stat_statements' in-memory counters).
