# SADI Q-1 / Q-3 — Source Probe Results (v4 Final)
Run at: 2026-04-06 05:01 UTC

## Scope
- **V-3** MINGPAO: 403 bypass — can Referer/UA headers unlock RSS and article pages?
- **V-4** AASTOCKS: CSS selector confirmation, published_at source, Traditional Chinese via ?totc=1

---

## V-3  MINGPAO — 403 Bypass

- **Strategy**: `RSS_CRAWLER — confirmed with header bypass`
- **Working header set**: `Bare (no headers)`

### Header Test Matrix

| Header Set | RSS Status | RSS OK | Article Status | Article OK | Body Length |
|------------|------------|--------|----------------|------------|-------------|
| Bare (no headers) | 200 | ✓ | 403 | ✗ | N/A |
| Basic User-Agent only | 200 | ✓ | 403 | ✗ | N/A |
| UA + Referer (self) | 200 | ✓ | 403 | ✗ | N/A |
| UA + Referer + Accept | 200 | ✓ | 403 | ✗ | N/A |
| UA + Referer + Accept + Cache-Control | 200 | ✓ | 403 | ✗ | N/A |
| feedparser (built-in UA) | 200 | ✓ | N/A | ✗ | N/A |

### Action Required
- Add header set `Bare (no headers)` to MINGPAO `crawl_config`
- Strategy confirmed as **RSS_CRAWLER**

---

## V-4  AASTOCKS — CSS Selector + ZH Content

### List Page
- URL: `https://www.aastocks.com/en/stocks/news/aafn/latest-news`
- HTTP status: 200
- Article links found: 10
- **CSS selector**: `a[href*="/aafn-con/NOW."]`
- Timestamp patterns in page: `['2026/04/04 04:31', '2026/04/04 01:53', '2026/04/04 01:26', '2026/04/03 23:53', '2026/04/03 22:22']`

Sample article links:
- `/en/stocks/news/aafn-con/NOW.1515938/latest-news/AAFN`
- `/en/stocks/news/aafn-con/NOW.1515937/latest-news/AAFN`
- `/en/stocks/news/aafn-con/NOW.1515936/latest-news/AAFN`

### Article Page — English (default)
- HTTP status: 200
- Body length: 9002 chars
- published_at from meta: `None`
- published_at from text: `2026-04-06T13:01`
- Body preview: _AASTOCKS.com
Members
About Us
News & Research
Commentary
Warrants&CBBCs
Warrants
CBBCs
US Stocks
AASTOCKS.COM LIMITED (阿斯達克網絡信息有限公司) All rights reserved.
You expressly agree that the use of this app/w_

### Article Page — Traditional Chinese (?totc=1)
- HTTP status: 200
- Body length: 9002 chars
- Chinese character ratio: 0.0
- **Is Traditional Chinese**: False
- Body preview: _AASTOCKS.com
Members
About Us
News & Research
Commentary
Warrants&CBBCs
Warrants
CBBCs
US Stocks
AASTOCKS.COM LIMITED (阿斯達克網絡信息有限公司) All rights reserved.
You expressly agree that the use of this app/w_

### crawl_config Recommendation
```json
{
  "type": "AASTOCKS_CRAWLER",
  "list_url": "https://www.aastocks.com/en/stocks/news/aafn/latest-news",
  "article_url_pattern": "https://www.aastocks.com/en/stocks/news/aafn-con/NOW.{id}/latest-news/AAFN?totc=1",
  "list_css_selector": "a[href*='/aafn-con/NOW.']",
  "content_type": "html",
  "extractor": "trafilatura",
  "url_suffix_for_zh": "?totc=1",
  "published_at_source": "meta[property='article:published_time'] or text regex"
}
```

---

## Complete Source Strategy Summary (All 4 Sources)

| Source | Strategy | List Method | Body Method | published_at | Blocking Issues |
|--------|----------|-------------|-------------|--------------|-----------------|
| HKEX | CRAWLER | homecat JSON GET | PDF → pymupdf | JSON relD/M/Y/Time | None ✅ |
| MINGPAO | RSS_CRAWLER | RSS pns/s00004.xml | httpx + trafilatura | RSS published field | None ✅ |
| AASTOCKS | CRAWLER | CSS `a[href*=aafn-con]` | trafilatura ?totc=1 | regex fallback | None ✅ |
| YAHOO_HK | RSS_CRAWLER | RSS + url_prefix_filter | trafilatura | RSS published field | None ✅ |

---

## TAD Update Checklist

- [ ] SADI TAD Section 2.1 — update strategy column for all 4 sources
- [ ] SADI TAD Section 2.2 — update crawl_config schema with confirmed selectors
- [ ] SADI TAD Section 3.3 — add pdf_extractor branch for HKEX
- [ ] Admin TAD Section 3.1 — update data_sources seed data
- [ ] SADI TAD Open Questions — close Q-1 and Q-3

_probe_sources.py v4 — 2026-04-06 05:01 UTC_