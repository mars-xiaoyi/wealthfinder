"""
Shared HTTP identity used by both fetch paths — Playwright (BrowserManager)
and httpx (the shared AsyncClient in app/main.py, used by PageCrawler for
AAStocks, HKEX's PDF phase, and Yahoo HK).

Realistic desktop Chrome UA + zh-HK Accept-Language so requests look like a
regular HK visitor. Without this, an unrealistic default UA (Playwright's
headless-shell tell, or httpx's `python-httpx/x.y.z`) gets fingerprinted and
blocked by some sources — Cloudflare, or a bare HTTP 429 — on the very first
request, regardless of request volume. Confirmed live against Yahoo HK; see
progress.md, 2026-08-31.
"""

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT_LANGUAGE = "zh-HK,zh;q=0.9,en;q=0.8"
