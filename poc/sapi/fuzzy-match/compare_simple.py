"""S8 against the Entity Analysis LLM Skill spike's simpler name search, on every case set here plus the 15 real
name arguments that spike's model sent to lookup_stock (its lookup_args.json). Neither matcher is tuned here.

The simpler search (its eval/harness.py `name_lookup_subseq`, run verbatim): normalize with a 7-character variant
map and a suffix regex, take every name that is an ordered subsequence of the query or the reverse (when at least
60% of the query's length), the shortest length difference wins, a tie between companies is a miss.

    <scratch venv>/bin/python compare_simple.py <llm-skill spike dir>
"""

import io
import json
import re
import sys
import types
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "data"))

import algorithms as alg
import eval_cases
import subseq
from evaluate import verdict
from hkex_holdout_cases import HKEX_CASES as HKEX_HOLDOUT_CASES

with redirect_stdout(io.StringIO()):
    import holdout_sep  # builds its case list; its own report is not needed here

LLM = Path(sys.argv[1]).resolve()
src = (LLM / "eval" / "harness.py").read_text()
harness = {"re": re, "common": types.SimpleNamespace(
    ENTRIES=alg.ENTRIES,
    _is_rmb_or_trading_only=lambda code, e: alg.is_rmb_counter(code, e) or alg.is_trading_only(e),
)}
exec(src[src.index("# ── C2: stronger code-side verification"):src.index("def verify_v2")], harness)

# Labels for the model's 15 name arguments: REITs are outside the equity list, 微億智造/東山精密 are not in this
# snapshot, and the foreign banks / 英偉達 have no HKEX listing, so all of those should miss.
REAL_ARG_LABELS = {"阿里巴巴": "09988", "騰訊控股": "00700", "碧桂園": "02007", "納真科技": "09856"}
real_args = [r["arg"] for r in json.loads((LLM / "lookup_args.json").read_text()) if not r["is_code"]]

SETS = {
    "475 labeled (dev + test)": [c[:3] for c in eval_cases.ALL],
    "held-out HKEX (49)": [c[:3] for c in HKEX_HOLDOUT_CASES],
    "held-out Sept news + HKEX (198)": holdout_sep.CASES,
    f"real lookup_stock name args ({len(real_args)})": [(a, REAL_ARG_LABELS.get(a), "real_arg") for a in real_args],
}
MATCHERS = {
    "LLM spike subseq": lambda q: (e := harness["name_lookup_subseq"](q)) and e["stock_code"],
    "S8": subseq.Matcher(dict(subseq.ROUNDS)["S8 + compactness before anchoring"]),
}

print("| Set | " + " | ".join(f"{m} c / w / m" for m in MATCHERS) + " |")
print("|---|" + "---|" * len(MATCHERS))
diffs = []
for set_name, cases in SETS.items():
    cells = []
    for m_name, fn in MATCHERS.items():
        cnt = Counter(verdict(exp, fn(q)) for q, exp, _ in cases)
        cells.append(f"{cnt['correct']} / {cnt['wrong']} / {cnt['missed']}")
    print(f"| {set_name} | " + " | ".join(cells) + " |")
    for q, exp, cat in cases:
        got = {m: fn(q) for m, fn in MATCHERS.items()}
        if len({verdict(exp, g) for g in got.values()}) > 1:
            diffs.append((set_name, q, exp, got))

lab = lambda c: f"{c} {alg.ENTRIES[c]['company_name_zh']}" if c else "—"  # noqa: E731
print("\nCases where the two disagree on the verdict:")
for set_name, q, exp, got in diffs:
    print(f"- [{set_name}] {q}: expected {lab(exp)}; " + "; ".join(f"{m} {lab(g)} ({verdict(exp, g)})"
                                                            for m, g in got.items()))

# HKEX announcements print the issuer's code, which EntityAnalysisSkill's code check resolves before any name
# search, so the name-search fallback mostly sees news-style names. Same comparison, split by case type.
print("\n| Case type (all sets) | " + " | ".join(f"{m} c / w / m" for m in MATCHERS) + " | S8 − simple, utility (c − 2w) |")
print("|---|" + "---|" * (len(MATCHERS) + 1))
groups = {"news-style names (news, hand-collected, real args)": lambda cat: not cat.startswith("hkex_legal"),
          "HKEX legal names (English + Chinese)": lambda cat: cat.startswith("hkex_legal")}
for g_name, keep in groups.items():
    cases = [c for cs in SETS.values() for c in cs if keep(c[2])]
    cells, util = [], []
    for fn in MATCHERS.values():
        cnt = Counter(verdict(exp, fn(q)) for q, exp, _ in cases)
        cells.append(f"{cnt['correct']} / {cnt['wrong']} / {cnt['missed']}")
        util.append(cnt["correct"] - 2 * cnt["wrong"])
    print(f"| {g_name} ({len(cases)}) | " + " | ".join(cells) + f" | {util[1] - util[0]:+d} |")
