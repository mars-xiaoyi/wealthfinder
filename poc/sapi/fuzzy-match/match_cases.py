"""Run every case in cases.py through implementation.md §4.2's lookup_stock/_fuzzy_match, as specified,
against the live HK Stock List snapshot (fetch_stock_list.py).

    python3 poc/sapi/fuzzy-match/match_cases.py
"""

import json
import re
from collections import Counter
from pathlib import Path

from cases import CASES

HERE = Path(__file__).resolve().parent
ENTRIES: dict[str, dict] = json.loads((HERE / "stock_list_snapshot.json").read_text())
_CODE_RE = re.compile(r"\d{2,5}(\.HK)?", re.IGNORECASE)


# ── §4.2, as written ────────────────────────────────────────────────────────────
def _normalize_name(text: str) -> str:
    return "".join(text.split()).casefold()


def _lcs_length(a: str, b: str) -> int:
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def _overlap_set(q: str, name: str) -> int:
    return len(set(q) & set(name))


def _overlap_counter(q: str, name: str) -> int:
    return sum((Counter(q) & Counter(name)).values())


def score_all(query: str, min_overlap_ratio: float, overlap_fn=_overlap_set) -> list[tuple[int, int, str, str]]:
    """Every (overlap, lcs, code, matched_name) passing the floor, in §4.2's final sort order."""
    q = _normalize_name(query)
    scored = []
    for code, entry in ENTRIES.items():
        for candidate_name in (entry["company_name_zh"], entry["company_name_en"]):
            name = _normalize_name(candidate_name)
            overlap = overlap_fn(q, name)
            if overlap / len(q) >= min_overlap_ratio:
                scored.append((overlap, _lcs_length(q, name), code, candidate_name))
    scored.sort(key=lambda t: (-t[0], -t[1]))  # stable: ties keep dict (= HKEX file) order
    return scored


def lookup_stock(name_or_code: str, min_overlap_ratio: float, overlap_fn=_overlap_set) -> str | None:
    text = name_or_code.strip()
    if _CODE_RE.fullmatch(text):
        code = text.upper().removesuffix(".HK").zfill(5)
        return code if code in ENTRIES else None
    if not text:
        return None
    scored = score_all(text, min_overlap_ratio, overlap_fn)
    return scored[0][2] if scored else None


# ── report ─────────────────────────────────────────────────────────────────────
def verdict(expected: str | None, actual: str | None) -> str:
    if expected == actual:
        return "correct"
    if actual is None:
        return "missed"
    return "wrong"


def label(code: str | None) -> str:
    return f"{code} {ENTRIES[code]['company_name_zh']}" if code else "—"


def main() -> None:
    ratios = (0.5, 0.6, 0.7, 0.8, 1.0)
    print("## Per-case results at the default floor 0.6 (set intersection, as specified)\n")
    print("| Query | Category | Expected | Actual | Verdict | Tie at top? | Top candidates (overlap, lcs) |")
    print("|---|---|---|---|---|---|---|")
    for query, expected, category, _ in CASES:
        scored = score_all(query, 0.6) if not _CODE_RE.fullmatch(query) else []
        actual = lookup_stock(query, 0.6)
        tie = len(scored) > 1 and scored[0][:2] == scored[1][:2]
        tied = [s for s in scored if s[:2] == scored[0][:2]] if scored else []
        top = "; ".join(f"{c} {n} ({o},{l})" for o, l, c, n in scored[:4])
        tie_txt = f"yes ({len(tied)}-way)" if tie else ""
        print(f"| {query} | {category} | {label(expected)} | {label(actual)} "
              f"| {verdict(expected, actual)} | {tie_txt} | {top} |")

    print("\n## Totals by floor and intersection\n")
    print("| Floor | Intersection | Correct | Wrong | Missed | Cases whose outcome differs from set@0.6 |")
    print("|---|---|---|---|---|---|")
    baseline = {q: lookup_stock(q, 0.6) for q, *_ in CASES}
    for ratio in ratios:
        for name, fn in (("set", _overlap_set), ("Counter", _overlap_counter)):
            counts = Counter()
            changed = []
            for query, expected, *_ in CASES:
                actual = lookup_stock(query, ratio, fn)
                counts[verdict(expected, actual)] += 1
                if actual != baseline[query]:
                    changed.append(f"{query}: {label(baseline[query])} → {label(actual)}")
            print(f"| {ratio} | {name} | {counts['correct']} | {counts['wrong']} | {counts['missed']} "
                  f"| {'<br>'.join(changed)} |")


if __name__ == "__main__":
    main()
