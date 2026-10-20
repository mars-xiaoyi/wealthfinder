import io
from collections.abc import Sequence
from types import SimpleNamespace

import httpx
import openpyxl
import pytest

from app.common import error_codes
from app.common import hk_stock_list as hk_stock_list_module
from app.common import http_client as http_client_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError
from app.common.hk_stock_list import HKStockListCache
from app.common.stock import Stock

EN_URL = hk_stock_list_module._ACTIVESTOCK_EN_URL
ZH_URL = hk_stock_list_module._ACTIVESTOCK_ZH_URL
SECURITIES_URL = hk_stock_list_module._LIST_OF_SECURITIES_URL
_URLS = {"en": EN_URL, "zh": ZH_URL, "securities": SECURITIES_URL}

EN_ENTRIES = [
    {"i": 1, "c": "00001", "n": "CKH HOLDINGS", "s": 1},
    {"i": 2, "c": "00005", "n": "HSBC HOLDINGS", "s": 2},
    {"i": 3, "c": "02800", "n": "TRACKER FUND", "s": 3},
]
ZH_ENTRIES = [
    {"i": 1, "c": "00001", "n": "長和", "s": 1},
    {"i": 3, "c": "00006", "n": "電能實業", "s": 3},
    {"i": 4, "c": "02800", "n": "盈富基金", "s": 4},
]
# (Stock Code, Name of Securities, Category) — 02911 is an equity in neither name file.
SECURITIES = [
    ("00001", "CKH HOLDINGS", "Equity"),
    ("00005", "HSBC HOLDINGS", "Equity"),
    ("00006", "POWER ASSETS", "Equity"),
    ("02800", "TRACKER FUND", "Exchange Traded Products"),
    ("02911", "NEW LISTING", "Equity"),
]

EXPECTED_ENTRIES = {
    "00001": Stock("00001", "HKEX", company_name_zh="長和", company_name_en="CKH HOLDINGS"),
    "00005": Stock("00005", "HKEX", company_name_zh="", company_name_en="HSBC HOLDINGS"),
    "00006": Stock("00006", "HKEX", company_name_zh="電能實業", company_name_en=""),
}


def _xlsx(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> bytes:
    """A List of Securities spreadsheet: HKEX's two title rows, then the header and rows."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(("List of Securities",))
    sheet.append(("Updated as at 20/10/2026",))
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


SECURITIES_XLSX = _xlsx(("Stock Code", "Name of Securities", "Category"), SECURITIES)
# HKEX renaming the category value: every row parses, but none is "Equity".
NO_EQUITIES_XLSX = _xlsx(
    ("Stock Code", "Name of Securities", "Category"),
    [(code, name, "Equities") for code, name, _category in SECURITIES],
)


def _responses(**overrides: object) -> dict[str, object]:
    """HKEX's three responses by URL, each valid unless overridden (keys: en, zh, securities).
    An override is a Response, an HTTPError to raise, or a list of those served in turn."""
    responses: dict[str, object] = {
        "en": httpx.Response(200, json=EN_ENTRIES),
        "zh": httpx.Response(200, json=ZH_ENTRIES),
        "securities": httpx.Response(200, content=SECURITIES_XLSX),
    }
    responses.update(overrides)
    return {
        EN_URL: responses["en"],
        ZH_URL: responses["zh"],
        SECURITIES_URL: responses["securities"],
    }


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch):
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", None)
    monkeypatch.setattr(http_client_module, "_http_client", None)


@pytest.fixture
async def hkex(monkeypatch):
    """Serves the shared http client's requests from a {url: Response | HTTPError | list}
    dict; assign hkex.responses to change them. hkex.requests counts requests per URL.
    Connection retries don't wait."""

    state = SimpleNamespace(responses=_responses(), requests={})

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        state.requests[url] = state.requests.get(url, 0) + 1
        result = state.responses[url]
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, httpx.HTTPError):
            raise result
        assert isinstance(result, httpx.Response)
        return result

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(http_client_module, "_http_client", client)
    monkeypatch.setattr(hk_stock_list_module, "_CONNECT_RETRY_WAITS_S", (0, 0))
    yield state
    await client.aclose()


@pytest.fixture
def loaded_cache(monkeypatch):
    """An already registered cache with one entry, as after an earlier successful refresh."""
    cache = HKStockListCache(
        {"00700": Stock("00700", "HKEX", company_name_zh="騰訊控股", company_name_en="TENCENT")}
    )
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", cache)
    return cache


UNREACHABLE = [
    pytest.param({key: httpx.HTTPError("unreachable")}, id=f"{key}_http_error")
    for key in ("en", "zh", "securities")
] + [
    pytest.param({key: httpx.Response(500)}, id=f"{key}_http_500")
    for key in ("en", "zh", "securities")
]

UNPARSEABLE = [
    pytest.param({"en": httpx.Response(200, json={"unexpected": "shape"})}, id="en_wrong_shape"),
    pytest.param({"zh": httpx.Response(200, json={"unexpected": "shape"})}, id="zh_wrong_shape"),
    pytest.param(
        {"securities": httpx.Response(200, content=_xlsx(("Code", "Name", "Type"), SECURITIES))},
        id="securities_header_renamed",
    ),
]


def get_hk_stock_list_cache_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        hk_stock_list_module.get_hk_stock_list_cache()


@pytest.mark.parametrize(
    "securities_xlsx",
    [
        pytest.param(SECURITIES_XLSX, id="hkex_layout"),
        pytest.param(
            _xlsx(
                ("Category", "Board Lot", "Stock Code"),
                [(category, "500", code) for code, _name, category in SECURITIES],
            ),
            id="columns_moved",
        ),
    ],
)
async def refresh_hk_stock_list_first_load(hkex, securities_xlsx):
    """Equities only, names joined by code: a name missing from one file is "", and an equity
    in neither file is skipped."""
    hkex.responses = _responses(securities=httpx.Response(200, content=securities_xlsx))

    entries_loaded = await hk_stock_list_module.refresh_hk_stock_list()

    cache = hk_stock_list_module.get_hk_stock_list_cache()
    assert entries_loaded == 3
    assert cache.entries == EXPECTED_ENTRIES


async def refresh_hk_stock_list_rebinds_loaded_cache(hkex, loaded_cache):
    old_entries = loaded_cache.entries
    snapshot = dict(old_entries)

    entries_loaded = await hk_stock_list_module.refresh_hk_stock_list()

    assert entries_loaded == 3
    assert hk_stock_list_module.get_hk_stock_list_cache() is loaded_cache
    assert loaded_cache.entries == EXPECTED_ENTRIES
    assert loaded_cache.entries is not old_entries
    assert old_entries == snapshot


@pytest.mark.parametrize("key", ["en", "zh", "securities"])
async def refresh_hk_stock_list_connect_error_retried(hkex, key):
    """A failed connection is retried; the third attempt succeeding loads the list."""
    refused = httpx.ConnectError("connection refused")
    hkex.responses = _responses(**{key: [refused, refused, _responses()[_URLS[key]]]})

    entries_loaded = await hk_stock_list_module.refresh_hk_stock_list()

    assert entries_loaded == 3
    assert hk_stock_list_module.get_hk_stock_list_cache().entries == EXPECTED_ENTRIES
    assert hkex.requests[_URLS[key]] == 3


@pytest.mark.parametrize("key", ["en", "zh", "securities"])
async def refresh_hk_stock_list_connect_error_exhausted(hkex, key):
    """Three failed connections give up as unreachable."""
    hkex.responses = _responses(**{key: httpx.ConnectError("connection refused")})

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert exc_info.value.error_code == error_codes.UPSTREAM_UNAVAILABLE
    assert hkex.requests[_URLS[key]] == 3
    assert len(hk_stock_list_module.get_hk_stock_list_cache()) == 0


@pytest.mark.parametrize("overrides", UNREACHABLE)
async def refresh_hk_stock_list_unreachable(hkex, overrides):
    hkex.responses = _responses(**overrides)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert exc_info.value.error_code == error_codes.UPSTREAM_UNAVAILABLE
    assert len(hk_stock_list_module.get_hk_stock_list_cache()) == 0


@pytest.mark.parametrize("overrides", UNPARSEABLE)
async def refresh_hk_stock_list_unparseable(hkex, overrides):
    hkex.responses = _responses(**overrides)

    with pytest.raises(ParseError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert len(hk_stock_list_module.get_hk_stock_list_cache()) == 0


async def refresh_hk_stock_list_no_equities(hkex):
    """A first load whose responses parse to zero equities fails, leaving the empty fallback."""
    hkex.responses = _responses(securities=httpx.Response(200, content=NO_EQUITIES_XLSX))

    with pytest.raises(ParseError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert len(hk_stock_list_module.get_hk_stock_list_cache()) == 0


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        pytest.param(
            {"en": httpx.HTTPError("unreachable")}, ServiceUnavailableError, id="unreachable"
        ),
        pytest.param(
            {"en": httpx.Response(200, json={"unexpected": "shape"})}, ParseError, id="unparseable"
        ),
        pytest.param(
            {"securities": httpx.Response(200, content=NO_EQUITIES_XLSX)},
            ParseError,
            id="no_equities",
        ),
    ],
)
async def refresh_hk_stock_list_failure_keeps_loaded_cache(hkex, loaded_cache, overrides, error):
    old_entries = loaded_cache.entries
    snapshot = dict(old_entries)
    hkex.responses = _responses(**overrides)

    with pytest.raises(error):
        await hk_stock_list_module.refresh_hk_stock_list()

    assert hk_stock_list_module.get_hk_stock_list_cache() is loaded_cache
    assert loaded_cache.entries is old_entries
    assert old_entries == snapshot
