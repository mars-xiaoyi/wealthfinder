"""Build SAPI's lookup_stock regression fixture from this spike's case sets and S8.

Every labeled name query in eval_cases.ALL, data/hkex_holdout_cases.py, data/sep_news_cases.py and
data/sep_hkex_cases.py, deduplicated by query (first label wins) and without stock codes (lookup_stock
searches by name only), is run through S8 on stock_list_snapshot.json. Rows whose correct code is not in
the snapshot are dropped. Each row records the correct outcome and what S8 returns; a row S8 gets wrong
or misses also records why (the failure categories of README.md "What S8 still gets wrong or misses").

Refresh the snapshot first (fetch_stock_list.py, SAPI's venv), then run with this spike's venv:
    <spike venv>/bin/python poc/sapi/fuzzy-match/make_lookup_stock_cases.py
"""

import contextlib
import io
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "data"))
OUT = HERE.parents[2] / "stock-assistant-pipeline-intelligence/tests/integration/app/common/lookup_stock_cases.json"

with contextlib.redirect_stdout(io.StringIO()):
    import algorithms as alg
    import eval_cases
    import hkex_holdout_cases
    import sep_hkex_cases
    import sep_news_cases
    import subseq

S8 = dict(subseq.ROUNDS)["S8 + compactness before anchoring"]
_LEGAL_WORD_RE = re.compile(r"\b(limited|ltd|co|inc|corporation|corp|plc)\b\.?", re.IGNORECASE)

# Rows whose recorded result a deliberate change to SAPI's name search has changed on purpose:
# {query: stock code or None}. Applied after running S8, so a regeneration keeps them.
OVERRIDES: dict[str, str | None] = {}


def _cases() -> dict[str, str | None]:
    """{query: correct stock code, or None for a company with no HKEX listing}."""
    rows = [(c[0], c[1]) for c in eval_cases.ALL]
    rows += [(c[0], c[1]) for c in hkex_holdout_cases.HKEX_CASES + sep_hkex_cases.HKEX_CASES]
    rows += list(sep_news_cases.SEP_NEWS_POSITIVES)
    rows += [(q, None) for q in sep_news_cases.SEP_NEWS_NEGATIVES]
    cases: dict[str, str | None] = {}
    for query, code in rows:
        cases.setdefault(query, code)
    return cases


def _top_codes(matcher: subseq.Matcher, query: str) -> set[str]:
    """Every stock code holding S8's best score for query (more than one is a tie)."""
    forms = subseq.forms_of(query, S8)
    exact = {e.code for e in matcher.entries for fm in e.forms if fm in forms}
    if exact:
        return exact
    q_latin = forms[0].isascii()
    if q_latin:
        pool = {id(e): e for fm in forms for t in fm.split() if len(t) >= 3
                for e in matcher.latin_index.get(t[:3], ())}.values()
    else:
        pool = [e for e in matcher.entries if not e.latin]
    scored = []
    for e in pool:
        keys = [k for q in forms for n in e.forms if (k := matcher._score(q, n, q_latin, e.latin)) is not None]
        if keys:
            scored.append((max(keys), e.code))
    if not scored:
        return set()
    top = max(k for k, _ in scored)
    return {c for k, c in scored if k == top}


def _reason(matcher: subseq.Matcher, query: str, correct: str | None, s8: str | None) -> str | None:
    if s8 == correct:
        return None
    if s8 is not None:
        return f"wrong code {s8}"
    tied = _top_codes(matcher, query)
    if len(tied) > 1:
        return "tie between " + "/".join(sorted(tied))
    if query.isascii() and _LEGAL_WORD_RE.search(query):
        return "English legal name"
    return "miss"


def main() -> None:
    matcher = subseq.Matcher(S8)
    rows = []
    for query, correct in _cases().items():
        if alg.code_lookup(query)[0] or (correct is not None and correct not in alg.ENTRIES):
            continue
        s8 = OVERRIDES[query] if query in OVERRIDES else matcher(query)
        rows.append({"query": query, "correct": correct, "s8": s8, "reason": _reason(matcher, query, correct, s8)})
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    wrong = sum(r["reason"] is not None for r in rows)
    print(f"{len(rows)} rows ({wrong} where S8 is wrong or misses) -> {OUT}")


main()
