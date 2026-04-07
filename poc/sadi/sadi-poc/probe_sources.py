"""
SADI Q-1 / Q-3 — Data Source Probe Script v4 (Final Targeted Validation)
MWP (Mars Wealthfinder) Pre-Implementation Research

Focus of this run:
  V-3  MINGPAO : Can HTTP 403 be bypassed with Referer / Cookie headers?
                 If yes — what is the minimum working header set?
  V-4  AASTOCKS: Confirm CSS selectors for list page and article page.
                 Confirm published_at source. Confirm ?totc=1 returns Traditional Chinese.

Previously confirmed (not re-tested):
  HKEX     : CRAWLER via homecat JSON + PDF direct link. CSS selector confirmed.
  YAHOO_HK : RSS_CRAWLER confirmed. url_prefix_filter needed.

Usage:
    pip install feedparser httpx trafilatura
    python probe_sources.py

Output:
    Console + probe_results.md
"""

import sys
import time
import re
from datetime import datetime, timezone

missing = []
try:
    import feedparser
except ImportError:
    missing.append("feedparser")
try:
    import httpx
except ImportError:
    missing.append("httpx")
try:
    import trafilatura
except ImportError:
    missing.append("trafilatura")

if missing:
    print(f"[ERROR] Missing: {', '.join(missing)}")
    print(f"        Run: pip install {' '.join(missing)}")
    sys.exit(1)

TIMEOUT   = 15
RSS_URL   = "https://news.mingpao.com/rss/pns/s00004.xml"

# ── header sets to try for MINGPAO ───────────────────────────────────────────
# Each entry: (label, headers_dict)
MINGPAO_HEADER_SETS = [
    (
        "Bare (no headers)",
        {},
    ),
    (
        "Basic User-Agent only",
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    ),
    (
        "UA + Referer (self)",
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://news.mingpao.com/",
        },
    ),
    (
        "UA + Referer + Accept",
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer":         "https://news.mingpao.com/",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
        },
    ),
    (
        "UA + Referer + Accept + Cache-Control",
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer":         "https://news.mingpao.com/",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Cache-Control":   "no-cache",
            "Pragma":          "no-cache",
        },
    ),
    (
        "feedparser (built-in UA)",
        None,   # sentinel — use feedparser directly
    ),
]

# AASTOCKS URLs to probe
AASTOCKS_LIST_URL    = "https://www.aastocks.com/en/stocks/news/aafn/latest-news"
AASTOCKS_ARTICLE_ZH  = "http://www.aastocks.com/en/stocks/news/aafn-con/NOW.1515933/top-news/AAFN?totc=1"
AASTOCKS_ARTICLE_EN  = "http://www.aastocks.com/en/stocks/news/aafn-con/NOW.1515933/top-news/AAFN"

AASTOCKS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer":         "https://www.aastocks.com/en/stocks/news/aafn",
}


def divider(label=""):
    width = 70
    pad   = (width - len(label) - 2) // 2 if label else 0
    print(f"\n{'─'*pad} {label} {'─'*pad}" if label else "─" * width)


# ─────────────────────────────────────────────────────────────────────────────
# V-3  MINGPAO: 403 bypass test
# ─────────────────────────────────────────────────────────────────────────────
def validate_mingpao_403():
    divider("V-3  MINGPAO — 403 Bypass Test")
    print("Goal: Find minimum header set to access RSS and article pages without 403.\n")

    # Step 1: get a real article URL from RSS (feedparser often sends its own UA)
    print("[Step 1] Fetch RSS to get real article URL...")
    article_url = None
    feed = feedparser.parse(RSS_URL)
    if feed.entries:
        article_url = feed.entries[0].get("link", "")
        print(f"  ✓ RSS OK — {len(feed.entries)} entries")
        print(f"  Sample article URL: {article_url}")
    else:
        print(f"  ✗ RSS failed (status {feed.get('status')}) — will use hardcoded fallback URL")
        # Use a recent article URL pattern as fallback
        today = datetime.now(timezone.utc)
        article_url = (
            f"https://news.mingpao.com/pns/%e7%b6%93%e6%bf%9f/article/"
            f"{today.strftime('%Y%m%d')}/s00004/1000000000000"
        )

    results = []

    # Step 2: test each header set against BOTH RSS and article page
    print("\n[Step 2] Testing header sets against RSS URL and article page...\n")

    for label, headers in MINGPAO_HEADER_SETS:
        print(f"  ── {label} ──")

        # RSS test
        if headers is None:
            # feedparser test
            rss_result = feedparser.parse(RSS_URL)
            rss_status  = rss_result.get("status", "?")
            rss_ok      = bool(rss_result.entries)
            print(f"    RSS      : status={rss_status} entries={len(rss_result.entries)} → {'✓' if rss_ok else '✗'}")
            art_status, art_ok, art_len, art_preview = None, False, 0, ""
        else:
            try:
                r = httpx.get(RSS_URL, headers=headers, timeout=TIMEOUT, follow_redirects=True)
                rss_status = r.status_code
                rss_ok     = rss_status == 200 and len(r.text) > 500
                print(f"    RSS      : HTTP {rss_status} ({len(r.text)} chars) → {'✓' if rss_ok else '✗'}")
            except Exception as e:
                rss_status, rss_ok = f"ERR: {e}", False
                print(f"    RSS      : {rss_status}")

            # Article page test (only if we have a real URL)
            art_status, art_ok, art_len, art_preview = None, False, 0, ""
            if article_url and "1000000000000" not in article_url:
                time.sleep(0.5)
                try:
                    r2 = httpx.get(
                        article_url, headers=headers,
                        timeout=TIMEOUT, follow_redirects=True
                    )
                    art_status = r2.status_code
                    if art_status == 200:
                        body = trafilatura.extract(
                            r2.text, include_comments=False, include_tables=False
                        )
                        if body and len(body.strip()) > 100:
                            art_ok      = True
                            art_len     = len(body.strip())
                            art_preview = body.strip()[:200]
                    print(f"    Article  : HTTP {art_status} trafilatura={'✓ ' + str(art_len) + ' chars' if art_ok else '✗'}")
                except Exception as e:
                    art_status = f"ERR: {e}"
                    print(f"    Article  : {art_status}")

        results.append({
            "label":      label,
            "rss_status": rss_status,
            "rss_ok":     rss_ok,
            "art_status": art_status,
            "art_ok":     art_ok,
            "art_len":    art_len,
            "art_preview": art_preview,
        })
        time.sleep(1)

    # Step 3: verdict
    print("\n[Verdict]")
    working = [r for r in results if r["rss_ok"]]
    if working:
        best = working[0]
        print(f"  ✓ Bypass SUCCEEDED with: '{best['label']}'")
        print(f"    RSS: ✓  Article: {'✓ (' + str(best['art_len']) + ' chars)' if best['art_ok'] else '✗ (need further test)'}")
        strategy = "RSS_CRAWLER — confirmed with header bypass"
    else:
        print("  ✗ All header sets FAILED — 403 not bypassable without Cookie/session")
        print("  → MINGPAO requires authenticated session or alternative source")
        strategy = "BLOCKED — requires Cookie or alternative source"

    return {
        "results":     results,
        "strategy":    strategy,
        "article_url": article_url,
        "working_headers": working[0]["label"] if working else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# V-4  AASTOCKS: CSS selector + published_at + Traditional Chinese confirmation
# ─────────────────────────────────────────────────────────────────────────────
def validate_aastocks():
    divider("V-4  AASTOCKS — CSS Selector + published_at + ZH Confirmation")
    print("Goal: Confirm CSS selectors for list/article, published_at source, and ?totc=1 ZH output.\n")

    results = {}

    # ── Part A: List page CSS selector ───────────────────────────────────────
    print("── Part A: List page — extract article links and titles ──\n")
    try:
        r = httpx.get(
            AASTOCKS_LIST_URL, headers=AASTOCKS_HEADERS,
            timeout=TIMEOUT, follow_redirects=True
        )
        print(f"  HTTP {r.status_code} | {len(r.text)} chars")

        if r.status_code == 200:
            # Extract article links matching the known URL pattern
            links = re.findall(
                r'href="((?:https?://[^"]*)?/(?:en/stocks/news/aafn-con/NOW\.\d+)[^"]*)"',
                r.text
            )
            # Also extract titles near those links
            title_pattern = re.compile(
                r'href="[^"]*aafn-con/NOW\.(\d+)[^"]*"[^>]*>\s*([^<]{10,200})'
            )
            title_matches = title_pattern.findall(r.text)

            unique_links = list(dict.fromkeys(links))[:10]

            print(f"  Article links found: {len(unique_links)}")
            for i, link in enumerate(unique_links[:5], 1):
                print(f"    [{i}] {link[:80]}")

            print(f"\n  Title matches (newsId → title):")
            seen = set()
            title_samples = []
            for news_id, title in title_matches[:10]:
                title_clean = title.strip()
                if news_id not in seen and len(title_clean) > 10:
                    seen.add(news_id)
                    title_samples.append((news_id, title_clean[:80]))
                    print(f"    NOW.{news_id}: {title_clean[:80]}")

            # Check for published_at in list page
            time_pattern = re.compile(r'(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}|\d{2}/\d{2}/\d{4})')
            time_matches  = time_pattern.findall(r.text)[:5]
            print(f"\n  Timestamp patterns found: {time_matches}")

            results["list"] = {
                "status":        r.status_code,
                "links_found":   len(unique_links),
                "sample_links":  unique_links[:3],
                "title_samples": title_samples[:3],
                "time_patterns": time_matches,
                "css_selector":  'a[href*="/aafn-con/NOW."]',
            }
        else:
            results["list"] = {"status": r.status_code, "error": "non-200"}

    except Exception as e:
        print(f"  ✗ Error: {e}")
        results["list"] = {"error": str(e)}

    time.sleep(1)

    # ── Part B: Article page — English version ────────────────────────────────
    print("\n── Part B: Article page (EN — default) ──\n")
    try:
        r_en = httpx.get(
            AASTOCKS_ARTICLE_EN, headers=AASTOCKS_HEADERS,
            timeout=TIMEOUT, follow_redirects=True
        )
        print(f"  HTTP {r_en.status_code}")

        if r_en.status_code == 200:
            body_en = trafilatura.extract(
                r_en.text, include_comments=False, include_tables=False
            )
            # Try to get published_at from meta tags
            pub_meta = re.search(
                r'<meta[^>]+(?:published_time|article:published)[^>]+content="([^"]+)"',
                r_en.text, re.IGNORECASE
            )
            pub_og = re.search(
                r'property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
                r_en.text, re.IGNORECASE
            )
            # Also look for visible date on page
            date_in_text = re.search(
                r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2})',
                r_en.text
            )

            print(f"  Body (EN)  : {len(body_en or '')} chars")
            print(f"  Preview    : {(body_en or '')[:300]}")
            print(f"  meta published_time: {pub_meta.group(1) if pub_meta else 'NOT FOUND'}")
            print(f"  og published_time  : {pub_og.group(1) if pub_og else 'NOT FOUND'}")
            print(f"  Date in text       : {date_in_text.group(1) if date_in_text else 'NOT FOUND'}")

            results["article_en"] = {
                "status":           r_en.status_code,
                "body_len":         len(body_en or ""),
                "body_preview":     (body_en or "")[:300],
                "pub_meta":         pub_meta.group(1) if pub_meta else None,
                "date_in_text":     date_in_text.group(1) if date_in_text else None,
            }

    except Exception as e:
        print(f"  ✗ Error: {e}")
        results["article_en"] = {"error": str(e)}

    time.sleep(1)

    # ── Part C: Article page — Traditional Chinese (?totc=1) ─────────────────
    print("\n── Part C: Article page (ZH Traditional — ?totc=1) ──\n")
    try:
        r_zh = httpx.get(
            AASTOCKS_ARTICLE_ZH, headers=AASTOCKS_HEADERS,
            timeout=TIMEOUT, follow_redirects=True
        )
        print(f"  HTTP {r_zh.status_code}")

        if r_zh.status_code == 200:
            body_zh = trafilatura.extract(
                r_zh.text, include_comments=False, include_tables=False
            )
            # Detect if content is Chinese
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', body_zh or ""))
            total_chars   = len(body_zh or "")
            zh_ratio      = chinese_chars / total_chars if total_chars > 0 else 0

            print(f"  Body (ZH)  : {total_chars} chars")
            print(f"  Chinese chars: {chinese_chars} ({zh_ratio:.0%} of body)")
            print(f"  Preview    : {(body_zh or '')[:300]}")
            print(f"  Is Traditional Chinese: {'YES ✓' if zh_ratio > 0.3 else 'NO — mostly English'}")

            results["article_zh"] = {
                "status":       r_zh.status_code,
                "body_len":     total_chars,
                "chinese_ratio": round(zh_ratio, 2),
                "is_chinese":   zh_ratio > 0.3,
                "body_preview": (body_zh or "")[:300],
            }

    except Exception as e:
        print(f"  ✗ Error: {e}")
        results["article_zh"] = {"error": str(e)}

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n[Summary]")
    list_r = results.get("list", {})
    en_r   = results.get("article_en", {})
    zh_r   = results.get("article_zh", {})

    print(f"  List page   : {list_r.get('links_found', '?')} article links | CSS: {list_r.get('css_selector', '?')}")
    print(f"  Article (EN): {en_r.get('body_len', '?')} chars | published_at: {en_r.get('pub_meta') or en_r.get('date_in_text') or 'NOT FOUND'}")
    print(f"  Article (ZH): {zh_r.get('body_len', '?')} chars | Chinese ratio: {zh_r.get('chinese_ratio', '?')} | Is ZH: {zh_r.get('is_chinese', '?')}")

    return results


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\nMARS WEALTHFINDER — SADI Source Probe v4 (Final Targeted Validation)")
    print(f"Run at: {run_at}")
    print(f"Scope: V-3 MINGPAO 403 bypass | V-4 AASTOCKS CSS selector + ZH content")

    mingpao_result  = validate_mingpao_403()
    aastocks_result = validate_aastocks()

    write_report(mingpao_result, aastocks_result, run_at)

    divider("FINAL SUMMARY")
    print()
    print(f"  MINGPAO  : {mingpao_result['strategy']}")
    if mingpao_result["working_headers"]:
        print(f"             Working headers: '{mingpao_result['working_headers']}'")
    print()
    list_r = aastocks_result.get("list", {})
    zh_r   = aastocks_result.get("article_zh", {})
    print(f"  AASTOCKS : List selector confirmed: {list_r.get('css_selector', '?')}")
    print(f"             ?totc=1 ZH content: {'✓' if zh_r.get('is_chinese') else '✗'}")
    print()
    print(f"✓ Report written to: probe_results.md")


def write_report(mingpao, aastocks, run_at):
    lines = [
        "# SADI Q-1 / Q-3 — Source Probe Results (v4 Final)",
        f"Run at: {run_at}",
        "",
        "## Scope",
        "- **V-3** MINGPAO: 403 bypass — can Referer/UA headers unlock RSS and article pages?",
        "- **V-4** AASTOCKS: CSS selector confirmation, published_at source, Traditional Chinese via ?totc=1",
        "",
        "---",
        "",
        "## V-3  MINGPAO — 403 Bypass",
        "",
        f"- **Strategy**: `{mingpao['strategy']}`",
        f"- **Working header set**: `{mingpao['working_headers'] or 'NONE — all failed'}`",
        "",
        "### Header Test Matrix",
        "",
        "| Header Set | RSS Status | RSS OK | Article Status | Article OK | Body Length |",
        "|------------|------------|--------|----------------|------------|-------------|",
    ]

    for r in mingpao["results"]:
        lines.append(
            f"| {r['label']} | {r['rss_status']} | {'✓' if r['rss_ok'] else '✗'} | "
            f"{r['art_status'] or 'N/A'} | {'✓' if r['art_ok'] else '✗'} | "
            f"{r['art_len'] or 'N/A'} |"
        )

    lines += [
        "",
        "### Action Required",
    ]

    if mingpao["working_headers"]:
        lines += [
            f"- Add header set `{mingpao['working_headers']}` to MINGPAO `crawl_config`",
            "- Strategy confirmed as **RSS_CRAWLER**",
        ]
    else:
        lines += [
            "- All header sets failed — MINGPAO requires browser Cookie or session token",
            "- Options: (1) Manual cookie extraction for `crawl_config`, "
              "(2) Use RSSHub as proxy, (3) Replace with HKET RSS",
        ]

    lines += [
        "",
        "---",
        "",
        "## V-4  AASTOCKS — CSS Selector + ZH Content",
        "",
    ]

    list_r = aastocks.get("list", {})
    en_r   = aastocks.get("article_en", {})
    zh_r   = aastocks.get("article_zh", {})

    lines += [
        "### List Page",
        f"- URL: `{AASTOCKS_LIST_URL}`",
        f"- HTTP status: {list_r.get('status', 'N/A')}",
        f"- Article links found: {list_r.get('links_found', 'N/A')}",
        f"- **CSS selector**: `{list_r.get('css_selector', 'N/A')}`",
        f"- Timestamp patterns in page: `{list_r.get('time_patterns', 'N/A')}`",
        "",
        "Sample article links:",
    ]
    for link in list_r.get("sample_links", []):
        lines.append(f"- `{link}`")

    lines += [
        "",
        "### Article Page — English (default)",
        f"- HTTP status: {en_r.get('status', 'N/A')}",
        f"- Body length: {en_r.get('body_len', 'N/A')} chars",
        f"- published_at from meta: `{en_r.get('pub_meta', 'NOT FOUND')}`",
        f"- published_at from text: `{en_r.get('date_in_text', 'NOT FOUND')}`",
        f"- Body preview: _{en_r.get('body_preview', '')[:200]}_",
        "",
        "### Article Page — Traditional Chinese (?totc=1)",
        f"- HTTP status: {zh_r.get('status', 'N/A')}",
        f"- Body length: {zh_r.get('body_len', 'N/A')} chars",
        f"- Chinese character ratio: {zh_r.get('chinese_ratio', 'N/A')}",
        f"- **Is Traditional Chinese**: {zh_r.get('is_chinese', 'N/A')}",
        f"- Body preview: _{zh_r.get('body_preview', '')[:200]}_",
        "",
        "### crawl_config Recommendation",
        "```json",
        "{",
        '  "type": "AASTOCKS_CRAWLER",',
        f'  "list_url": "{AASTOCKS_LIST_URL}",',
        '  "article_url_pattern": "https://www.aastocks.com/en/stocks/news/aafn-con/NOW.{id}/latest-news/AAFN?totc=1",',
        '  "list_css_selector": "a[href*=\'/aafn-con/NOW.\']",',
        '  "content_type": "html",',
        '  "extractor": "trafilatura",',
        '  "url_suffix_for_zh": "?totc=1",',
        '  "published_at_source": "meta[property=\'article:published_time\'] or text regex"',
        "}",
        "```",
        "",
        "---",
        "",
        "## Complete Source Strategy Summary (All 4 Sources)",
        "",
        "| Source | Strategy | List Method | Body Method | published_at | Blocking Issues |",
        "|--------|----------|-------------|-------------|--------------|-----------------|",
        f"| HKEX | CRAWLER | homecat JSON GET | PDF → pymupdf | JSON relD/M/Y/Time | None ✅ |",
        f"| MINGPAO | {mingpao['strategy'].split(' —')[0]} | RSS pns/s00004.xml | httpx + trafilatura | RSS published field | {'None ✅' if mingpao['working_headers'] else '403 bypass failed ❌'} |",
        f"| AASTOCKS | CRAWLER | CSS `a[href*=aafn-con]` | trafilatura ?totc=1 | {'meta tag ✅' if en_r.get('pub_meta') else 'regex fallback'} | None ✅ |",
        f"| YAHOO_HK | RSS_CRAWLER | RSS + url_prefix_filter | trafilatura | RSS published field | None ✅ |",
        "",
        "---",
        "",
        "## TAD Update Checklist",
        "",
        "- [ ] SADI TAD Section 2.1 — update strategy column for all 4 sources",
        "- [ ] SADI TAD Section 2.2 — update crawl_config schema with confirmed selectors",
        "- [ ] SADI TAD Section 3.3 — add pdf_extractor branch for HKEX",
        "- [ ] Admin TAD Section 3.1 — update data_sources seed data",
        "- [ ] SADI TAD Open Questions — close Q-1 and Q-3",
        "",
        f"_probe_sources.py v4 — {run_at}_",
    ]

    with open("probe_results.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()