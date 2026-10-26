import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
import opencc
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

_T2S = opencc.OpenCC("t2s")
_COUNTER_MARKER_RE = re.compile(r"[－\-][A-ZＡ-Ｚ]{1,3}$")  # －Ｗ, -SW, －Ｂ …
_FULLWIDTH = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})
_SPACE_BETWEEN_LATIN_RE = re.compile(r"(?<=[a-z0-9])\s+(?=[a-z0-9])")
_EN_WORD_SPLIT_RE = re.compile(r"[\s.,]+")
_ZH_LEGAL_SUFFIX_RE = re.compile(r"(股份)?有限公司$|公司$")
# Matched after t2s, so only tails that read the same in simplified Chinese are stripped.
_ZH_GENERIC_TAIL_RE = re.compile(r"(控股|股份)+$")
_EN_LEGAL_WORDS = {
    "limited",
    "ltd",
    "co",
    "company",
    "corporation",
    "corp",
    "inc",
    "plc",
    "llc",
    "the",
}
_EN_GENERIC_WORDS = {
    "holdings", "holding", "hldg", "hldgs", "group", "gp",
    "international", "intl", "int'l", "and", "&",
}  # fmt: skip

# A Latin word needs this many letters to index a name, to match a longer word as its prefix,
# or (summed over a name's words) to match inside a query.
_LATIN_PREFIX_LEN = 3
_CJK_MIN_QUERY_LEN = 2
# A listed name inside a longer mention needs at least this share of the mention's length.
_NAME_IN_QUERY_MIN_RATIO = 0.6
# A name search slower than this logs at WARNING instead of INFO; not env-configurable.
_SLOW_CALL_THRESHOLD_MS = 20


@dataclass(frozen=True)
class NameEntry:
    """One of a stock's listed names, normalized for matching."""

    stock_code: str
    # Distinct, longest first: the full name, without its legal suffix, without its generic tail.
    variants: list[str]
    latin: bool


@dataclass(frozen=True)
class StockNameIndex:
    """The HK Stock List's names, normalized and grouped for matching."""

    names: list[NameEntry]
    names_by_code: dict[str, list[NameEntry]]
    names_by_latin_prefix: dict[str, list[NameEntry]]  # first letters of an English word


class HKStockListCache:
    """The process-wide in-memory HK Stock List, keyed by native stock code, with its names
    prepared for matching."""

    def __init__(self, entries: dict[str, Stock]) -> None:
        self.entries = entries
        self.name_index = _build_name_index(entries)

    async def reload(self, entries: dict[str, Stock]) -> None:
        """Replace the list with entries, together with its name index."""
        # Building the index takes CPU time; a worker thread keeps it off the shared event loop.
        name_index = await asyncio.to_thread(_build_name_index, entries)
        # Rebind both back to back with no await between, never mutate in place: a reader sees
        # one list's entries and name index together, and a reader mid-iteration over the old
        # ones keeps a consistent snapshot.
        self.entries = entries
        self.name_index = name_index

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


def _normalize(text: str) -> str:
    """Counter marker dropped, full-width to ASCII, traditional to simplified, case-folded;
    whitespace removed except one space between two Latin words."""
    text = _T2S.convert(_COUNTER_MARKER_RE.sub("", text).translate(_FULLWIDTH)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return _SPACE_BETWEEN_LATIN_RE.sub("\x00", text).replace(" ", "").replace("\x00", " ")


def _name_variants(raw: str) -> list[str]:
    """
    The variants of a company name compared when matching, distinct and longest first: the
    normalized name, then without its legal suffix, then also without its generic tail
    (騰訊控股有限公司 -> 腾讯控股有限公司, 腾讯控股, 腾讯). Empty variants are dropped.
    """
    full = _normalize(raw)
    if full.isascii():
        legal = " ".join(t for t in _EN_WORD_SPLIT_RE.split(full) if t and t not in _EN_LEGAL_WORDS)
        generic = " ".join(t for t in legal.split() if t not in _EN_GENERIC_WORDS)
    else:
        legal = _ZH_LEGAL_SUFFIX_RE.sub("", full)
        generic = _ZH_GENERIC_TAIL_RE.sub("", legal)
    return list(dict.fromkeys(v for v in (full, legal, generic) if v))


def _build_name_index(entries: dict[str, Stock]) -> StockNameIndex:
    """The name index of a stock list: every distinct non-empty name of every stock."""
    names: list[NameEntry] = []
    names_by_code: dict[str, list[NameEntry]] = {}
    names_by_latin_prefix: dict[str, list[NameEntry]] = {}
    for code, stock in entries.items():
        for raw in dict.fromkeys((stock.company_name_zh, stock.company_name_en)):
            variants = _name_variants(raw)
            if not variants:
                continue
            entry = NameEntry(stock_code=code, variants=variants, latin=variants[0].isascii())
            names.append(entry)
            names_by_code.setdefault(code, []).append(entry)
            if entry.latin:
                prefixes = {
                    w[:_LATIN_PREFIX_LEN]
                    for v in variants
                    for w in v.split()
                    if len(w) >= _LATIN_PREFIX_LEN
                }
                for prefix in prefixes:
                    names_by_latin_prefix.setdefault(prefix, []).append(entry)
    return StockNameIndex(
        names=names, names_by_code=names_by_code, names_by_latin_prefix=names_by_latin_prefix
    )


def _is_subsequence(short: str, long: str) -> bool:
    chars = iter(long)
    return all(ch in chars for ch in short)


def _subsequence_span(short: str, long: str) -> tuple[int, int] | None:
    """The tightest [start, end) of long holding short as an ordered subsequence: the
    earliest-ending match, started as late as that end allows. None when there is no match."""
    i = 0
    end = None
    for pos, ch in enumerate(long):
        if ch == short[i]:
            i += 1
            if i == len(short):
                end = pos + 1
                break
    if end is None:
        return None
    j = len(short) - 1
    k = end - 1
    while j >= 0:
        if long[k] == short[j]:
            j -= 1
        k -= 1
    return k + 1, end


def _latin_word_hits(words: list[str], others: list[str]) -> int:
    """How many of words appear in others, a word also matching a longer word it is a prefix of."""
    return sum(
        any(o == w or (len(w) >= _LATIN_PREFIX_LEN and o.startswith(w)) for o in others)
        for w in words
    )


def _score(q: str, n: str) -> tuple[float, ...] | None:
    """How well query variant q matches name variant n; a higher tuple is a better match, None
    is none."""
    q_latin, n_latin = q.isascii(), n.isascii()
    if q_latin != n_latin:
        return None
    if q_latin:
        qw, nw = q.split(), n.split()
        name_hits = _latin_word_hits(nw, qw)
        if name_hits == len(nw) and sum(len(w) for w in nw) >= _LATIN_PREFIX_LEN:
            return (4, name_hits / len(qw), len(nw))  # the whole name is in the query
        query_hits = _latin_word_hits(qw, nw)
        if query_hits == len(qw):
            return (3, query_hits / len(nw), -len(n))  # the whole query is in the name
        return None
    if len(q) < _CJK_MIN_QUERY_LEN:
        return None
    q_chars, n_chars = set(q), set(n)
    if not (q_chars <= n_chars or n_chars <= q_chars):
        return None
    if len(q) <= len(n):
        span = _subsequence_span(q, n)
        if span:
            start, end = span
            return (5, len(q) / (end - start), int(start == 0), len(q) / len(n), -len(n))
    if 3 <= len(n) < len(q) and _subsequence_span(n, q):
        return (4, len(n) / len(q), len(n))
    return None


def _scoring_pool(index: StockNameIndex, variants: list[str]) -> list[NameEntry]:
    """The names worth scoring against a query: those sharing an English word's first letters
    with an English query, every Chinese name for a Chinese one."""
    if not variants:
        return []
    if variants[0].isascii():
        pool = {
            id(e): e
            for v in variants
            for w in v.split()
            if len(w) >= _LATIN_PREFIX_LEN
            for e in index.names_by_latin_prefix.get(w[:_LATIN_PREFIX_LEN], [])
        }
        return list(pool.values())
    return [e for e in index.names if not e.latin]


def _best_scoring_codes(pool: list[NameEntry], variants: list[str]) -> set[str]:
    """The stock codes whose names score highest against the query; empty if none matches."""
    best_score: tuple[float, ...] | None = None
    best_codes: set[str] = set()
    for entry in pool:
        scores = [s for q in variants for n in entry.variants if (s := _score(q, n)) is not None]
        if not scores:
            continue
        score = max(scores)
        if best_score is None or score > best_score:
            best_score, best_codes = score, {entry.stock_code}
        elif score == best_score:
            best_codes.add(entry.stock_code)
    return best_codes


def _search(index: StockNameIndex, name: str) -> str | None:
    """
    The stock code a company name refers to, or None on a miss. An exact variant match decides
    on its own; otherwise the best-scoring name wins. Matches on more than one stock code are a
    miss. Logs each call's cost.
    """
    start = time.perf_counter()
    variants = _name_variants(name)
    exact_codes = {e.stock_code for e in index.names if set(e.variants) & set(variants)}
    if exact_codes:
        candidates, codes = index.names, exact_codes
    else:
        candidates = _scoring_pool(index, variants)
        codes = _best_scoring_codes(candidates, variants)
    code = next(iter(codes)) if len(codes) == 1 else None

    latency_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if latency_ms > _SLOW_CALL_THRESHOLD_MS else logging.INFO
    logger.log(
        level,
        "lookup_stock_completed",
        name=name,
        candidates_scanned=len(candidates),
        latency_ms=latency_ms,
        found=code is not None,
    )
    return code


def lookup_stock(name: str) -> Stock | None:
    """
    The listed stock a company name refers to. Returns None on a miss: no match, or a tie
    between companies.
    """
    hk_stock_list_cache = get_hk_stock_list_cache()
    code = _search(hk_stock_list_cache.name_index, name)
    return hk_stock_list_cache.entries[code] if code else None


def name_matches_stock(name: str, stock: Stock) -> bool:
    """
    Whether a company name fits a listed stock's names: it abbreviates one of them, or contains
    one that makes up most of it. False for a stock not on the list.
    """
    listed = get_hk_stock_list_cache().name_index.names_by_code.get(stock.stock_code, [])
    for q in _name_variants(name):
        for entry in listed:
            for n in entry.variants:
                if _is_subsequence(q, n):
                    return True
                if len(n) >= _NAME_IN_QUERY_MIN_RATIO * len(q) and _is_subsequence(n, q):
                    return True
    return False


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


def _is_extra_counter(code: str, zh_name: str, en_name: str) -> bool:
    """An RMB counter (80700 騰訊控股－Ｒ) or a trading-only line (－Ｔ / -T) of a company
    listed under another code."""
    return (
        (code.startswith("8") and zh_name.endswith("Ｒ"))
        or zh_name.endswith("Ｔ")
        or en_name.endswith("-T")
    )


def _parse_hk_stock_list(
    en_content: bytes, zh_content: bytes, securities_content: bytes
) -> dict[str, Stock]:
    """Join the two name files by stock code and keep equities only, without the extra counters
    of a company listed under another code. A name missing from one file becomes ""; an equity
    code in neither file is skipped."""
    en_names = _parse_names(en_content)
    zh_names = _parse_names(zh_content)
    entries: dict[str, Stock] = {}
    for code in _parse_equity_codes(securities_content):
        if code not in en_names and code not in zh_names:
            continue
        zh_name = zh_names.get(code, "")
        en_name = en_names.get(code, "")
        if _is_extra_counter(code, zh_name, en_name):
            continue
        entries[code] = Stock(
            stock_code=code, exchange=_EXCHANGE, company_name_zh=zh_name, company_name_en=en_name
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
    call registers the cache, later calls reload it. Returns the
    number of entries loaded.
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
            # Built in a worker thread, like reload(), to keep the index off the event loop.
            _hk_stock_list_cache = await asyncio.to_thread(HKStockListCache, new_entries)
        else:
            await _hk_stock_list_cache.reload(new_entries)
    except Exception:
        if _hk_stock_list_cache is None:
            _hk_stock_list_cache = HKStockListCache({})
        raise
    return len(new_entries)
