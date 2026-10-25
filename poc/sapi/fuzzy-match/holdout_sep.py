"""Score the matchers, unchanged, once on articles published 2026-09-01 to 09-07 (cleaned_news_export.csv).

Only companies that no earlier case set uses. Pass bar, fixed before scoring: S8 correct beats WRatio@90's
correct, and S8 wrong ≤ 3% of all cases.

    <scratch venv>/bin/python holdout_sep.py
"""

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "data"))

import algorithms as alg
import subseq
from cases import CASES as HAND_CASES
from evaluate import verdict
from hkex_cases import HKEX_CASES
from hkex_holdout_cases import HKEX_CASES as HKEX_HOLDOUT_CASES
from news_cases import NEWS_POSITIVES
from sep_hkex_cases import HKEX_CASES as SEP_HKEX_CASES
from sep_news_cases import SEP_NEWS_NEGATIVES, SEP_NEWS_POSITIVES

USED = ({c[1] for c in HAND_CASES + HKEX_CASES + HKEX_HOLDOUT_CASES if c[1]}
        | {code for _, code in NEWS_POSITIVES})
LISTED = json.loads((HERE / "stock_list_snapshot.json").read_text())

CASES = [(q, code, "news_hk") for q, code in SEP_NEWS_POSITIVES]
CASES += [(q, None, "news_non_hk") for q in SEP_NEWS_NEGATIVES]
CASES += [(q, code, cat) for q, code, cat, _ in SEP_HKEX_CASES]
CASES = [c for c in CASES if c[1] is None or (c[1] in LISTED and c[1] not in USED)]

rounds = dict(subseq.ROUNDS)
FNS = {
    "spec §4.2": alg.spec,
    "WRatio@90": alg.LibraryScorer(alg.SCORERS["WRatio"], 90),
    "S8 (final)": subseq.Matcher(rounds["S8 + compactness before anchoring"]),
}
CATS = ("news_hk", "news_non_hk", "hkex_legal_en", "hkex_legal_zh")
print(f"{len(CASES)} held-out cases, {len({c[1] for c in CASES if c[1]})} companies, "
      + ", ".join(f"{cat} {sum(c[2] == cat for c in CASES)}" for cat in CATS) + "\n")
print("| Matcher | all c / w / m | " + " | ".join(f"{cat} c / w / m" for cat in CATS) + " |")
print("|---|---|" + "---|" * len(CATS))
totals = {}
for name, fn in FNS.items():
    cnt = {k: Counter() for k in ("all", *CATS)}
    for q, exp, cat in CASES:
        v = verdict(exp, fn(q))
        cnt["all"][v] += 1
        cnt[cat][v] += 1
    totals[name] = cnt["all"]
    cell = lambda c: f"{c['correct']} / {c['wrong']} / {c['missed']}"  # noqa: E731
    print(f"| {name} | {cell(cnt['all'])} | " + " | ".join(cell(cnt[cat]) for cat in CATS) + " |")

s8, wr = totals["S8 (final)"], totals["WRatio@90"]
wrong_rate = s8["wrong"] / len(CASES)
print(f"\nPass bar: S8 correct {s8['correct']} > WRatio correct {wr['correct']}: {s8['correct'] > wr['correct']}; "
      f"S8 wrong rate {wrong_rate:.1%} ≤ 3%: {wrong_rate <= 0.03}")

print("\nS8 failures:")
for q, exp, cat in CASES:
    act = FNS["S8 (final)"](q)
    if act != exp:
        lab = lambda c: f"{c} {alg.ENTRIES[c]['company_name_zh']}" if c else "—"  # noqa: E731
        print(f"- {verdict(exp, act)}: {q} [{cat}] expected {lab(exp)}, got {lab(act)}")
