"""Every labeled case, split deterministically into dev (tune on it) and test (report only).

A case is (query, expected code or None, category, source). The split hashes the expected stock code (the query
itself for no-match cases), so every surface form of one company lands in the same half and the halves share
no company. results_rounds_query_split.md holds the earlier split that hashed the query string.
"""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "data"))

from cases import CASES as HAND_CASES  # noqa: E402
from news_cases import NEWS_NEGATIVES, NEWS_POSITIVES, TRADING_ONLY_NEGATIVES  # noqa: E402

try:
    from hkex_cases import HKEX_CASES  # noqa: E402
except ImportError:
    HKEX_CASES = []

Case = tuple[str, str | None, str, str]


def _all_cases() -> list[Case]:
    cases: dict[str, Case] = {}

    def add(case: Case) -> None:
        cases.setdefault(case[0], case)  # first label wins; the sources agree on every shared query

    for case in HAND_CASES:
        add(case)
    for mention, code in NEWS_POSITIVES:
        add((mention, code, "news_hk", "news name(code)"))
    for mention in NEWS_NEGATIVES:
        add((mention, None, "news_non_hk", "news name(US/A ticker)"))
    for mention in TRADING_ONLY_NEGATIVES:
        add((mention, None, "trading_only", "news name(US ticker), HKEX -T line only"))
    for case in HKEX_CASES:
        add(case)
    return list(cases.values())


def _listed(cases: list[Case]) -> list[Case]:
    """Drop positives whose code is outside the equity list (REITs, bonds), which lookup_stock can never return."""
    listed = json.loads((HERE / "stock_list_snapshot.json").read_text())
    return [c for c in cases if c[1] is None or c[1] in listed]


def _split(case: Case) -> str:
    key = case[1] or case[0]
    return "dev" if hashlib.sha256(key.encode()).digest()[0] % 2 == 0 else "test"


ALL = _listed(_all_cases())
DEV = [c for c in ALL if _split(c) == "dev"]
TEST = [c for c in ALL if _split(c) == "test"]
