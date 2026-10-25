"""Score the matchers, unchanged, on cases from days none of the rounds ever saw (data/hkex_holdout_cases.py).

    <scratch venv>/bin/python holdout.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "data"))

import algorithms as alg
import subseq
from evaluate import verdict
from hkex_holdout_cases import HKEX_CASES

rounds = dict(subseq.ROUNDS)
FNS = {
    "spec §4.2": alg.spec,
    "WRatio@90": alg.LibraryScorer(alg.SCORERS["WRatio"], 90),
    "S5": subseq.Matcher(rounds["S5 + generic tail strip (控股/集團/Holdings)"]),
    "S8 (final)": subseq.Matcher(rounds["S8 + compactness before anchoring"]),
}
print(f"{len(HKEX_CASES)} held-out cases, {len({c[1] for c in HKEX_CASES})} companies\n")
print("| Matcher | all c / w / m | English legal c / w / m | Chinese legal c / w / m |")
print("|---|---|---|---|")
for name, fn in FNS.items():
    cnt = {k: Counter() for k in ("all", "hkex_legal_en", "hkex_legal_zh")}
    for q, exp, cat, _ in HKEX_CASES:
        v = verdict(exp, fn(q))
        cnt["all"][v] += 1
        cnt[cat][v] += 1
    cell = lambda c: f"{c['correct']} / {c['wrong']} / {c['missed']}"  # noqa: E731
    print(f"| {name} | {cell(cnt['all'])} | {cell(cnt['hkex_legal_en'])} | {cell(cnt['hkex_legal_zh'])} |")

print("\nS8 failures:")
for q, exp, cat, _ in HKEX_CASES:
    act = FNS["S8 (final)"](q)
    if act != exp:
        lab = lambda c: f"{c} {alg.ENTRIES[c]['company_name_zh']}" if c else "—"  # noqa: E731
        print(f"- {verdict(exp, act)}: {q} [{cat}] expected {lab(exp)}, got {lab(act)}")
