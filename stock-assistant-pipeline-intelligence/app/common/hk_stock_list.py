import asyncio
import io
import json

import httpx
import openpyxl

from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError
from app.common.http_client import get_http_client
from app.common.stock import Stock
from app.logger import get_logger

logger = get_logger(__name__)

_ACTIVESTOCK_EN_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
_ACTIVESTOCK_ZH_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_c.json"
_LIST_OF_SECURITIES_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)

_EXCHANGE = "HKEX"
_STOCK_CODE_HEADER = "Stock Code"
_CATEGORY_HEADER = "Category"
_EQUITY_CATEGORY = "Equity"

# HKEX drops a few percent of fresh TLS handshakes, so a failed connection is retried: one
# wait before each retry, so len + 1 attempts in all.
_CONNECT_RETRY_WAITS_S = (1, 2)


class HKStockListCache:
    """The process-wide in-memory HK Stock List, keyed by native stock code."""

    def __init__(self, entries: dict[str, Stock]) -> None:
        self.entries = entries

    def __len__(self) -> int:
        return len(self.entries)


_hk_stock_list_cache: HKStockListCache | None = None


def get_hk_stock_list_cache() -> HKStockListCache:
    """
    Return the process's one HKStockListCache instance.
    Raises ServiceUnavailableError if called before refresh_hk_stock_list() has run.
    """
    if _hk_stock_list_cache is None:
        raise ServiceUnavailableError(
            "get_hk_stock_list_cache() called before refresh_hk_stock_list() initialized it",
            category=ErrorCategory.STOCK_LIST,
        )
    return _hk_stock_list_cache


def _parse_names(content: bytes) -> dict[str, str]:
    """{stock_code: name} from one HKEXnews activestock JSON file."""
    return {entry["c"]: entry["n"] for entry in json.loads(content)}


def _parse_equity_codes(content: bytes) -> set[str]:
    """Stock codes whose Category is Equity in the List of Securities spreadsheet.

    Columns are located by header name, so a moved column is still found and a renamed one
    fails. reset_dimensions(): in read-only mode openpyxl trusts the file's recorded sheet
    size, which HKEX's file understates, and would otherwise stop after a few rows.
    """
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    try:
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        for row in rows:
            if _STOCK_CODE_HEADER in row and _CATEGORY_HEADER in row:
                code_col = row.index(_STOCK_CODE_HEADER)
                category_col = row.index(_CATEGORY_HEADER)
                break
        else:
            raise ParseError(
                "List of Securities header row not found", category=ErrorCategory.STOCK_LIST
            )
        return {str(row[code_col]) for row in rows if row[category_col] == _EQUITY_CATEGORY}
    finally:
        workbook.close()


def _parse_hk_stock_list(
    en_content: bytes, zh_content: bytes, securities_content: bytes
) -> dict[str, Stock]:
    """Join the two name files by stock code and keep equities only. A name missing from one
    file becomes ""; an equity code in neither file is skipped."""
    en_names = _parse_names(en_content)
    zh_names = _parse_names(zh_content)
    entries: dict[str, Stock] = {}
    for code in _parse_equity_codes(securities_content):
        if code not in en_names and code not in zh_names:
            continue
        entries[code] = Stock(
            stock_code=code,
            exchange=_EXCHANGE,
            company_name_zh=zh_names.get(code, ""),
            company_name_en=en_names.get(code, ""),
        )
    return entries


async def _get(http_client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET url, retrying when the connection itself fails. Any other httpx error (a timeout, a
    non-2xx status) is raised at once: retrying a slow HKEX would multiply HTTP_TIMEOUT_S."""
    for attempt, wait_s in enumerate(_CONNECT_RETRY_WAITS_S, start=1):
        try:
            return await http_client.get(url)
        except httpx.ConnectError:
            logger.warning("hkex_connect_retry", url=url, attempt=attempt, wait_s=wait_s)
            await asyncio.sleep(wait_s)
    return await http_client.get(url)


async def _fetch_and_filter_hk_stock_list(http_client: httpx.AsyncClient) -> dict[str, Stock]:
    """
    Fetch HKEXnews's bilingual stock lists and the List of Securities, and return
    {native_code: Stock} for equities only.
    Raises ServiceUnavailableError (COMMON-5002) on any httpx.HTTPError (after retrying a
    failed connection, see _get()), or ParseError
    (COMMON-6000) on any failure while parsing a successful response.
    """
    try:
        en_resp = await _get(http_client, _ACTIVESTOCK_EN_URL)
        en_resp.raise_for_status()
        zh_resp = await _get(http_client, _ACTIVESTOCK_ZH_URL)
        zh_resp.raise_for_status()
        securities_resp = await _get(http_client, _LIST_OF_SECURITIES_URL)
        securities_resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ServiceUnavailableError.upstream_unavailable(
            f"HKEXnews unreachable: {exc!r}", category=ErrorCategory.STOCK_LIST
        ) from exc

    try:
        # Parsing is synchronous (the spreadsheet alone takes ~0.4 s); a worker thread keeps
        # it off the event loop the layers and routes share.
        return await asyncio.to_thread(
            _parse_hk_stock_list, en_resp.content, zh_resp.content, securities_resp.content
        )
    except ParseError:
        raise
    except Exception as exc:
        # Deliberately broad: any failure here means the response shape changed.
        raise ParseError(
            f"HK Stock List response shape mismatch: {exc!r}", category=ErrorCategory.STOCK_LIST
        ) from exc


async def refresh_hk_stock_list() -> int:
    """
    Fetch the HK Stock List and load it into the process-wide HKStockListCache: the first
    call registers the cache, later calls rebind its entries. Returns the number of entries
    loaded.
    On any failure, installs an empty HKStockListCache if none is registered yet, then
    re-raises; an already loaded cache is left untouched. A response with zero equities is a
    ParseError too.
    """
    global _hk_stock_list_cache
    try:
        new_entries = await _fetch_and_filter_hk_stock_list(get_http_client())
        if not new_entries:
            # HKEX always lists equities, so zero means a changed cell value (e.g. a renamed
            # category); loading it would wipe a loaded cache.
            raise ParseError(
                "HK Stock List parsed to zero equities", category=ErrorCategory.STOCK_LIST
            )
        if _hk_stock_list_cache is None:
            _hk_stock_list_cache = HKStockListCache(new_entries)
        else:
            # Rebind, never mutate in place: a reader mid-iteration over the old dict keeps a
            # consistent snapshot.
            _hk_stock_list_cache.entries = new_entries
    except Exception:
        if _hk_stock_list_cache is None:
            _hk_stock_list_cache = HKStockListCache({})
        raise
    return len(new_entries)
