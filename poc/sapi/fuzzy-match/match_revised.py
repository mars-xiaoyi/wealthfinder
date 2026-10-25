"""Candidate fixes to §4.2's _fuzzy_match, applied one at a time on top of each other,
scored against the same cases as match_cases.py.

    python3 poc/sapi/fuzzy-match/match_revised.py
"""

import re
from collections import Counter

from cases import CASES
from match_cases import ENTRIES, _CODE_RE, _lcs_length, label, verdict

# R1: RMB counters (Trading Currency = RMB in the List of Securities) duplicate their HKD counter.
RMB_COUNTERS = {c for c, e in ENTRIES.items() if c.startswith("8") and e["company_name_zh"].endswith("Ｒ")}

# R2: HKEX stock-name markers (－Ｗ weighted voting rights, －Ｓ secondary listing, －Ｂ biotech, －Ｒ RMB, …)
_MARKER_RE = re.compile(r"[－\-][A-ZＡ-Ｚ]{1,3}$")

# R3: HK-news vs HKEX character variants seen in the case set
_VARIANTS = str.maketrans({"恆": "恒", "滙": "匯"})


def _normalize(text: str, strip_markers: bool, fold_variants: bool) -> str:
    if strip_markers:
        text = _MARKER_RE.sub("", text)
    if fold_variants:
        text = text.translate(_VARIANTS)
    return "".join(text.split()).casefold()


def lookup(query: str, *, r1=False, r2=False, r3=False, r4=False, r5=False, r6=False, r7=False,
           ratio: float = 0.6) -> str | None:
    text = query.strip()
    if _CODE_RE.fullmatch(text):
        code = text.upper().removesuffix(".HK").zfill(5)
        return code if code in ENTRIES else None
    q = _normalize(text, r2, r3)
    ascii_query = r6 and q.isascii()
    scored = []
    for code, entry in ENTRIES.items():
        if r1 and code in RMB_COUNTERS:
            continue
        best = None
        for candidate_name in (entry["company_name_zh"], entry["company_name_en"]):
            name = _normalize(candidate_name, r2, r3)
            if ascii_query:
                # R6: Latin letters overlap with almost anything; require the whole query inside the name
                if q not in name:
                    continue
                key = (len(q), len(q), 1, -len(name))
            else:
                overlap = sum((Counter(q) & Counter(name)).values()) if r4 else len(set(q) & set(name))
                if overlap / len(q) < ratio:
                    continue
                lcs = _lcs_length(q, name)
                # R5: then a contiguous match, then the shorter (more fully covered) name
                key = (overlap, lcs, int(q in name), -len(name)) if r5 else (overlap, lcs)
            if best is None or key > best:
                best = key
        if best is not None:
            scored.append((best, code))
    if not scored:
        return None
    scored.sort(key=lambda t: t[0], reverse=True)
    if r7 and len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # R7: a tie between different companies is ambiguous — report a miss
    return scored[0][1]


STEPS = [
    ("baseline (§4.2 as written)", {}),
    ("+R1 drop RMB counters", {"r1": True}),
    ("+R2 strip HKEX name markers", {"r1": True, "r2": True}),
    ("+R3 fold 恆/滙 variants", {"r1": True, "r2": True, "r3": True}),
    ("+R4 Counter intersection", {"r1": True, "r2": True, "r3": True, "r4": True}),
    ("+R5 tie-break: contiguous, then shorter name", {"r1": True, "r2": True, "r3": True, "r4": True, "r5": True}),
    ("+R6 Latin query: whole-query substring", {"r1": True, "r2": True, "r3": True, "r4": True, "r5": True, "r6": True}),
    ("+R7 remaining tie → miss", {"r1": True, "r2": True, "r3": True, "r4": True, "r5": True, "r6": True, "r7": True}),
]


def main() -> None:
    print("| Step | Correct | Wrong | Missed |")
    print("|---|---|---|---|")
    for title, flags in STEPS:
        counts = Counter(verdict(exp, lookup(q, **flags)) for q, exp, *_ in CASES)
        print(f"| {title} | {counts['correct']} | {counts['wrong']} | {counts['missed']} |")

    final = STEPS[-1][1]
    for ratio in (0.5, 0.6, 0.7, 0.8, 1.0):
        counts = Counter(verdict(exp, lookup(q, **final, ratio=ratio)) for q, exp, *_ in CASES)
        print(f"all fixes @ floor {ratio}: {dict(counts)}")

    print("\n## Remaining failures with every fix (floor 0.6)\n")
    print("| Query | Category | Expected | Actual | Verdict |")
    print("|---|---|---|---|---|")
    for q, exp, cat, _ in CASES:
        act = lookup(q, **final)
        if act != exp:
            print(f"| {q} | {cat} | {label(exp)} | {label(act)} | {verdict(exp, act)} |")


if __name__ == "__main__":
    main()


# ── Revised algorithm with names normalized once at load time, as a cache would hold them ──────────
INDEX = [
    (code, name, Counter(name))
    for code, entry in ENTRIES.items() if code not in RMB_COUNTERS
    for name in {_normalize(entry["company_name_zh"], True, True), _normalize(entry["company_name_en"], True, True)}
    if name
]


def lookup_indexed(query: str, ratio: float = 0.6) -> str | None:
    """Same result as lookup(query, **every fix) — only the per-name normalization moved to load time."""
    text = query.strip()
    if _CODE_RE.fullmatch(text):
        code = text.upper().removesuffix(".HK").zfill(5)
        return code if code in ENTRIES else None
    q = _normalize(text, True, True)
    q_counter = Counter(q)
    floor = ratio * len(q)
    best: dict[str, tuple] = {}
    for code, name, name_counter in INDEX:
        if q.isascii():
            if q not in name:
                continue
            key = (len(q), len(q), 1, -len(name))
        else:
            overlap = sum((q_counter & name_counter).values())
            if overlap < floor:
                continue
            key = (overlap, _lcs_length(q, name), int(q in name), -len(name))
        if code not in best or key > best[code]:
            best[code] = key
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda t: t[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]
