"""Score matchers on the dev and test halves of eval_cases.

Utility = correct − 2 × wrong: a wrong match is accepted silently, while a miss sends found=false and the LLM
retries with another name or the code, so a wrong costs more than a miss. Cutoffs are tuned on dev only.

    <scratch venv>/bin/python evaluate.py [matcher-name ...] [--failures NAME]
"""

import sys
from collections import Counter

import algorithms as alg
from eval_cases import ALL, DEV, TEST


def verdict(expected, actual) -> str:
    if expected == actual:
        return "correct"
    return "missed" if actual is None else "wrong"


def score(fn, cases) -> Counter:
    c = Counter(verdict(exp, fn(q)) for q, exp, *_ in cases)
    c["utility"] = c["correct"] - 2 * c["wrong"]
    return c


def tuned(name, factory, grid):
    """Pick the cutoff with the best dev utility."""
    best = max(grid, key=lambda g: score(factory(g), DEV)["utility"])
    return f"{name} (cutoff {best})", factory(best)


def matchers() -> dict:
    found = {"spec §4.2": alg.spec, "revised R1–R7": alg.revised_r1_r7}
    for name, scorer in alg.SCORERS.items():
        grid = [0.5, 0.6, 0.7, 0.8, 0.9] if name in ("levenshtein", "jaro_winkler") else [50, 60, 70, 80, 90]
        label, fn = tuned(name, lambda g, s=scorer: alg.LibraryScorer(s, g), grid)
        found[label] = fn
    label, fn = tuned("tfidf 1-2gram cosine", alg.TfidfCosine, [0.3, 0.4, 0.5, 0.6, 0.7])
    found[label] = fn
    import subseq
    for name, flags in subseq.ROUNDS:
        found[name] = subseq.Matcher(flags)
    return found


def row(name, fn) -> str:
    d, t = score(fn, DEV), score(fn, TEST)
    return (f"| {name} | {d['correct']} / {d['wrong']} / {d['missed']} | {d['utility']} "
            f"| {t['correct']} / {t['wrong']} / {t['missed']} | {t['utility']} |")


def main() -> None:
    args = sys.argv[1:]
    failures_for = args[args.index("--failures") + 1] if "--failures" in args else None
    print(f"{len(ALL)} cases: dev {len(DEV)}, test {len(TEST)} "
          f"(positives {sum(1 for c in ALL if c[1])}, negatives {sum(1 for c in ALL if not c[1])})\n")
    print("| Matcher | dev correct / wrong / missed | dev utility | test correct / wrong / missed | test utility |")
    print("|---|---|---|---|---|")
    all_m = matchers()
    for name, fn in all_m.items():
        print(row(name, fn))
    if failures_for:
        fn = next(f for n, f in all_m.items() if n.startswith(failures_for))
        which = TEST if "--test" in args else DEV
        print(f"\n## {'test' if which is TEST else 'dev'} failures: {failures_for}\n")
        for q, exp, cat, _ in which:
            act = fn(q)
            if act != exp:
                lab = lambda c: f"{c} {alg.ENTRIES[c]['company_name_zh']}" if c else "—"  # noqa: E731
                print(f"- {verdict(exp, act)}: {q} [{cat}] expected {lab(exp)}, got {lab(act)}")


if __name__ == "__main__":
    main()
