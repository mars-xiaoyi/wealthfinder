import copy
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
from app.common.hk_stock_list import HKStockListCache, NameEntry, StockNameIndex
from app.common.stock import Stock

EN_URL = hk_stock_list_module._ACTIVESTOCK_EN_URL
ZH_URL = hk_stock_list_module._ACTIVESTOCK_ZH_URL
SECURITIES_URL = hk_stock_list_module._LIST_OF_SECURITIES_URL
_URLS = {"en": EN_URL, "zh": ZH_URL, "securities": SECURITIES_URL}

EN_ENTRIES = [
    {"i": 1, "c": "00001", "n": "CKH HOLDINGS", "s": 1},
    {"i": 2, "c": "00005", "n": "HSBC HOLDINGS", "s": 2},
    {"i": 3, "c": "02800", "n": "TRACKER FUND", "s": 3},
    {"i": 4, "c": "80001", "n": "CKH HOLDINGS-R", "s": 4},
    {"i": 5, "c": "02991", "n": "CKH HOLDINGS-T", "s": 5},
    {"i": 6, "c": "02992", "n": "HSBC HOLDINGS-T", "s": 6},
    {"i": 7, "c": "06603", "n": "IFBH", "s": 7},
]
ZH_ENTRIES = [
    {"i": 1, "c": "00001", "n": "長和", "s": 1},
    {"i": 3, "c": "00006", "n": "電能實業", "s": 3},
    {"i": 4, "c": "02800", "n": "盈富基金", "s": 4},
    {"i": 5, "c": "80001", "n": "長和－Ｒ", "s": 5},
    {"i": 6, "c": "02991", "n": "長和－Ｔ", "s": 6},
    {"i": 7, "c": "06603", "n": "IFBH", "s": 7},
]
# (Stock Code, Name of Securities, Category) — 02911 is an equity in neither name file; 80001 is
# an RMB counter, 02991 a trading-only line named in both files, 02992 one named in English only;
# 06603 has the same name in both files.
SECURITIES = [
    ("00001", "CKH HOLDINGS", "Equity"),
    ("00005", "HSBC HOLDINGS", "Equity"),
    ("00006", "POWER ASSETS", "Equity"),
    ("02800", "TRACKER FUND", "Exchange Traded Products"),
    ("02911", "NEW LISTING", "Equity"),
    ("80001", "CKH HOLDINGS-R", "Equity"),
    ("02991", "CKH HOLDINGS-T", "Equity"),
    ("02992", "HSBC HOLDINGS-T", "Equity"),
    ("06603", "IFBH", "Equity"),
]

EMPTY_NAME_INDEX = StockNameIndex(names=[], names_by_code={}, names_by_latin_prefix={})

EXPECTED_ENTRIES = {
    "00001": Stock("00001", "HKEX", company_name_zh="長和", company_name_en="CKH HOLDINGS"),
    "00005": Stock("00005", "HKEX", company_name_zh="", company_name_en="HSBC HOLDINGS"),
    "00006": Stock("00006", "HKEX", company_name_zh="電能實業", company_name_en=""),
    "06603": Stock("06603", "HKEX", company_name_zh="IFBH", company_name_en="IFBH"),
}
_CKH_ZH = NameEntry("00001", ["长和"], latin=False)
_CKH_EN = NameEntry("00001", ["ckh holdings", "ckh"], latin=True)
_HSBC_EN = NameEntry("00005", ["hsbc holdings", "hsbc"], latin=True)
_POWER_ASSETS_ZH = NameEntry("00006", ["电能实业"], latin=False)
_IFBH = NameEntry("06603", ["ifbh"], latin=True)
# EXPECTED_ENTRIES' names: one entry per distinct non-empty name, normalized into its variants.
EXPECTED_NAME_INDEX = StockNameIndex(
    names=[_CKH_ZH, _CKH_EN, _HSBC_EN, _POWER_ASSETS_ZH, _IFBH],
    names_by_code={
        "00001": [_CKH_ZH, _CKH_EN],
        "00005": [_HSBC_EN],
        "00006": [_POWER_ASSETS_ZH],
        "06603": [_IFBH],
    },
    names_by_latin_prefix={
        "ckh": [_CKH_EN],
        "hol": [_CKH_EN, _HSBC_EN],
        "hsb": [_HSBC_EN],
        "ifb": [_IFBH],
    },
)


def _assert_name_index(index: StockNameIndex, expected: StockNameIndex) -> None:
    """Asserts index holds exactly expected's entries. A list mixing stocks follows the order
    entries were loaded in, which the parser does not fix, so it is compared by contents."""
    assert index.names_by_code == expected.names_by_code
    assert len(index.names) == len(expected.names)
    assert all(entry in index.names for entry in expected.names)
    assert index.names_by_latin_prefix.keys() == expected.names_by_latin_prefix.keys()
    for prefix, entries in expected.names_by_latin_prefix.items():
        assert len(index.names_by_latin_prefix[prefix]) == len(entries)
        assert all(entry in index.names_by_latin_prefix[prefix] for entry in entries)


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
    entries = {
        "00700": Stock("00700", "HKEX", company_name_zh="騰訊控股", company_name_en="TENCENT")
    }
    cache = HKStockListCache(entries)
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
    """Equities only, names joined by code: a name missing from one file is "", an equity in
    neither file is skipped, and so are RMB counters and trading-only lines. The name index
    holds the loaded entries' names."""
    hkex.responses = _responses(securities=httpx.Response(200, content=securities_xlsx))

    entries_loaded = await hk_stock_list_module.refresh_hk_stock_list()

    cache = hk_stock_list_module.get_hk_stock_list_cache()
    assert entries_loaded == 4
    assert cache.entries == EXPECTED_ENTRIES
    _assert_name_index(cache.name_index, EXPECTED_NAME_INDEX)


async def refresh_hk_stock_list_rebinds_loaded_cache(hkex, loaded_cache):
    old_entries = loaded_cache.entries
    old_name_index = loaded_cache.name_index
    snapshot = dict(old_entries)
    index_snapshot = copy.deepcopy(old_name_index)

    entries_loaded = await hk_stock_list_module.refresh_hk_stock_list()

    assert entries_loaded == 4
    assert hk_stock_list_module.get_hk_stock_list_cache() is loaded_cache
    assert loaded_cache.entries == EXPECTED_ENTRIES
    assert loaded_cache.entries is not old_entries
    assert old_entries == snapshot
    _assert_name_index(loaded_cache.name_index, EXPECTED_NAME_INDEX)
    assert old_name_index == index_snapshot


@pytest.mark.parametrize("key", ["en", "zh", "securities"])
async def refresh_hk_stock_list_connect_error_retried(hkex, key):
    """A failed connection is retried; the third attempt succeeding loads the list."""
    refused = httpx.ConnectError("connection refused")
    hkex.responses = _responses(**{key: [refused, refused, _responses()[_URLS[key]]]})

    entries_loaded = await hk_stock_list_module.refresh_hk_stock_list()

    assert entries_loaded == 4
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
    assert hk_stock_list_module.get_hk_stock_list_cache().entries == {}
    assert hk_stock_list_module.get_hk_stock_list_cache().name_index == EMPTY_NAME_INDEX


@pytest.mark.parametrize("overrides", UNREACHABLE)
async def refresh_hk_stock_list_unreachable(hkex, overrides):
    hkex.responses = _responses(**overrides)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert exc_info.value.error_code == error_codes.UPSTREAM_UNAVAILABLE
    assert hk_stock_list_module.get_hk_stock_list_cache().entries == {}
    assert hk_stock_list_module.get_hk_stock_list_cache().name_index == EMPTY_NAME_INDEX


@pytest.mark.parametrize("overrides", UNPARSEABLE)
async def refresh_hk_stock_list_unparseable(hkex, overrides):
    hkex.responses = _responses(**overrides)

    with pytest.raises(ParseError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert hk_stock_list_module.get_hk_stock_list_cache().entries == {}
    assert hk_stock_list_module.get_hk_stock_list_cache().name_index == EMPTY_NAME_INDEX


async def refresh_hk_stock_list_no_equities(hkex):
    """A first load whose responses parse to zero equities fails, leaving the empty fallback."""
    hkex.responses = _responses(securities=httpx.Response(200, content=NO_EQUITIES_XLSX))

    with pytest.raises(ParseError) as exc_info:
        await hk_stock_list_module.refresh_hk_stock_list()

    assert exc_info.value.category is ErrorCategory.STOCK_LIST
    assert hk_stock_list_module.get_hk_stock_list_cache().entries == {}
    assert hk_stock_list_module.get_hk_stock_list_cache().name_index == EMPTY_NAME_INDEX


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
    old_name_index = loaded_cache.name_index
    snapshot = dict(old_entries)
    hkex.responses = _responses(**overrides)

    with pytest.raises(error):
        await hk_stock_list_module.refresh_hk_stock_list()

    assert hk_stock_list_module.get_hk_stock_list_cache() is loaded_cache
    assert loaded_cache.entries is old_entries
    assert old_entries == snapshot
    assert loaded_cache.name_index is old_name_index


def _stocks(*rows: tuple[str, str, str]) -> dict[str, Stock]:
    return {code: Stock(code, "HKEX", zh, en) for code, zh, en in rows}


# 06001 and 06002 share both names; 01972 and 09901 both start with 太古; 09901 has no
# English name; 03988 and 02388 both contain 中 and 銀.
STOCKS = _stocks(
    ("00700", "騰訊控股", "TENCENT HOLDINGS"),
    ("09988", "阿里巴巴－ＳＷ", "BABA-SW"),  # noqa: RUF001
    ("01288", "農業銀行", "AGRICULTURAL BANK OF CHINA"),
    ("03988", "中國銀行", "BANK OF CHINA"),
    ("02388", "中銀香港", "BOC HONG KONG"),
    ("02628", "中國人壽", "CHINA LIFE"),
    ("00268", "金蝶國際", "KINGDEE INT'L"),
    ("01972", "太古地產", "SWIRE PROPERTIES"),
    ("09901", "太古可樂", ""),
    ("06001", "星河科技", "GALAXY TECH"),
    ("06002", "星河科技", "GALAXY TECH"),
)


@pytest.fixture
def stock_list(monkeypatch):
    """A loaded HK Stock List holding STOCKS."""
    cache = HKStockListCache(STOCKS)
    monkeypatch.setattr(hk_stock_list_module, "_hk_stock_list_cache", cache)


@pytest.mark.parametrize(
    ("name", "code"),
    [
        pytest.param("騰訊控股", "00700", id="exact"),
        pytest.param("騰訊控股有限公司", "00700", id="zh_legal_suffix"),
        pytest.param("騰訊", "00700", id="zh_generic_tail"),
        pytest.param("騰訊 控股", "00700", id="zh_space_removed"),
        pytest.param("阿里巴巴", "09988", id="listed_counter_marker"),
        pytest.param("ＴＥＮＣＥＮＴ Holdings Ltd.", "00700", id="en_fullwidth_legal_suffix"),  # noqa: RUF001
        pytest.param("Bank of  China", "03988", id="en_spaces_collapsed"),
        pytest.param("星河科技", None, id="exact_tie"),
        pytest.param("農行", "01288", id="zh_abbreviation"),
        pytest.param("中銀", "02388", id="zh_most_compact_span_wins"),
        pytest.param("中國人壽保險", "02628", id="zh_name_in_query"),
        pytest.param("太古", None, id="zh_tie"),
        pytest.param("銀農", None, id="zh_out_of_order"),
        pytest.param("騰", None, id="zh_too_short"),
        pytest.param("火星探險虛構公司", None, id="zh_no_match"),
        pytest.param("ABC有限公司", None, id="zh_query_latin_variant"),
        pytest.param("Agricultural Bank of China Hong Kong Branch", "01288", id="en_name_in_query"),
        pytest.param("Swire Prop", "01972", id="en_query_prefix_of_name"),
        pytest.param("Swire Pacific", None, id="en_no_match"),
        pytest.param("AB", None, id="en_no_indexed_word"),
        pytest.param("", None, id="blank"),
    ],
)
def lookup_stock_result(stock_list, name, code):
    """A match returns the list's own entry; a miss (no match, or a tie between companies)
    returns None."""
    stock = hk_stock_list_module.lookup_stock(name)

    if code is None:
        assert stock is None
    else:
        assert stock is STOCKS[code]


@pytest.mark.parametrize(
    ("name", "code", "matches"),
    [
        pytest.param("農行", "01288", True, id="abbreviation"),
        pytest.param("Agricultural Bank", "01288", True, id="en_abbreviation"),
        pytest.param("金蝶國際軟件", "00268", True, id="listed_name_in_name"),
        pytest.param("金蝶國際軟件集團有限公司", "00268", False, id="listed_name_too_short"),
        pytest.param("匯豐", "00700", False, id="unrelated"),
    ],
)
def name_matches_stock_result(stock_list, name, code, matches):
    assert hk_stock_list_module.name_matches_stock(name, STOCKS[code]) is matches


def name_matches_stock_not_listed(stock_list):
    stock = Stock("00005", "HKEX", company_name_zh="匯豐控股", company_name_en="HSBC HOLDINGS")

    assert hk_stock_list_module.name_matches_stock("匯豐控股", stock) is False
