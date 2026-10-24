"""Per-category results and latency for the finalists, over every labeled case (dev + test).

    <scratch venv>/bin/python compare.py
"""

import statistics
import time
from collections import Counter, defaultdict

import algorithms as alg
import subseq
from eval_cases import ALL
from evaluate import verdict

rounds = dict(subseq.ROUNDS)
FNS = {
    "spec §4.2": alg.spec,
    "WRatio@90": alg.LibraryScorer(alg.SCORERS["WRatio"], 90),
    "subsequence (S8)": subseq.Matcher(rounds["S8 + compactness before anchoring"]),
}

cats = sorted({c[2] for c in ALL})
res = {n: defaultdict(Counter) for n in FNS}
for n, fn in FNS.items():
    for q, exp, cat, _ in ALL:
        res[n][cat][verdict(exp, fn(q))] += 1

print("correct / wrong / missed\n")
print("| Category | n | " + " | ".join(FNS) + " |")
print("|---|---|" + "---|" * len(FNS))
for cat in cats:
    n = sum(res["spec §4.2"][cat].values())
    print(f"| {cat} | {n} | " + " | ".join(
        f"{res[k][cat]['correct']} / {res[k][cat]['wrong']} / {res[k][cat]['missed']}" for k in FNS) + " |")
tot = {k: sum((res[k][c] for c in cats), Counter()) for k in FNS}
print(f"| **all** | {len(ALL)} | " + " | ".join(
    f"**{tot[k]['correct']} / {tot[k]['wrong']} / {tot[k]['missed']}**" for k in FNS) + " |")

print("\n| Matcher | p50 ms | p95 ms | p99 ms | max ms |")
print("|---|---|---|---|---|")
qs = [q for q, *_ in ALL]
for n, fn in FNS.items():
    s = []
    for _ in range(5):
        for q in qs:
            t = time.perf_counter()
            fn(q)
            s.append((time.perf_counter() - t) * 1000)
    p = statistics.quantiles(s, n=100, method="inclusive")
    print(f"| {n} | {p[49]:.2f} | {p[94]:.2f} | {p[98]:.2f} | {max(s):.2f} |")
