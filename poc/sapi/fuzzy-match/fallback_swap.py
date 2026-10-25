"""Re-score the Entity Analysis LLM Skill spike's stored C3 runs with only the name-search fallback swapped.

EntityAnalysisSkill (impl.md §8.2, b59dbe8) checks the model's code first (code on the list and names agree, or the
article prints the code) and calls lookup_stock(name) only when that fails. This takes the model's "code|name" pairs
stored by that spike, keeps its code checks verbatim, and swaps the fallback: none, that spike's own
shortest-name subsequence search, §4.2, WRatio@90, S8. No API calls; nothing here is tuned.

    <scratch venv>/bin/python fallback_swap.py <llm-skill spike dir>

<llm-skill spike dir> is poc/sapi/llm-skill from branch sapi-2882704929-llm-skill-spike, with its gitignored
eval/eval_set_verify.json and eval/eval_set_nocode.json present.
"""

import json
import re
import sys
import types
from collections import Counter
from pathlib import Path

import algorithms as alg
import subseq

LLM = Path(sys.argv[1]).resolve()
EVAL = LLM / "eval"

# The LLM spike's code checks and its own fallback, taken verbatim from its eval/harness.py
# (the "C2: stronger code-side verification" block), so only the fallback differs.
common = types.SimpleNamespace(
    ENTRIES=alg.ENTRIES,
    _is_rmb_or_trading_only=lambda code, e: alg.is_rmb_counter(code, e) or alg.is_trading_only(e),
)
src = (EVAL / "harness.py").read_text()
block = src[src.index("# ── C2: stronger code-side verification"):src.index("def verify_v2")]
harness = {"re": re, "common": common}
exec(block, harness)
MAX_BODY_CHARS = int(re.search(r"MAX_BODY_CHARS = ([\d_]+)", src).group(1))

rounds = dict(subseq.ROUNDS)
_wratio = alg.LibraryScorer(alg.SCORERS["WRatio"], 90)
_s8 = subseq.Matcher(rounds["S8 + compactness before anchoring"])
FALLBACKS = {
    "none": lambda name: None,
    "LLM spike subseq": lambda name: (e := harness["name_lookup_subseq"](name)) and e["stock_code"],
    "§4.2 as written": alg.spec,
    "WRatio@90": _wratio,
    "S8": _s8,
}


def verify(code: str | None, name: str, text: str, fallback) -> tuple[str | None, bool]:
    """(resolved code, whether the fallback decided it)."""
    code = (code or "").strip().upper().removesuffix(".HK")
    if code.isdigit():
        entry = alg.ENTRIES.get(code.zfill(5))
        if entry and (harness["name_compatible"](name, entry) or harness["code_printed"](int(code), text)):
            return entry["stock_code"], False
    return fallback(name), True


def load(run: str, eval_set: str) -> list[tuple[dict, list[str], str]]:
    arts = {a["id"]: a for a in json.loads((EVAL / eval_set).read_text())["eval"]}
    adjud = json.loads((EVAL / "adjudication.json").read_text())
    out = []
    for f in sorted((EVAL / "runs" / run).glob("*.json")):
        rec = json.loads(f.read_text())
        art = arts[rec["article"]]
        gold = rec["gold"] if rec["gold"] is not None else art["gold"]
        gold = gold if gold is not None else adjud.get(rec["article"])
        if gold is None:
            continue
        out.append((rec, gold, art["title"] + art["body"][:MAX_BODY_CHARS]))
    return out


for label, run, eval_set in (("127 cleaned-news articles", "C3_lite31_vf", "eval_set_verify.json"),
                             ("no-code worst case", "C3_lite31_nc", "eval_set_nocode.json")):
    data = load(run, eval_set)
    print(f"## {label} ({run}, {len(data)} scored articles)\n")
    print("| Fallback | P | R | F1 | fallback hits right / wrong |")
    print("|---|---|---|---|---|")
    details = {}
    for fb_name, fb in FALLBACKS.items():
        tp = n_out = n_gold = 0
        hits = Counter()
        rows = []
        for rec, gold, text in data:
            codes = set()
            for _, key, _ in rec["lookups"]:
                code, name = key.split("|", 1)
                got, by_fallback = verify(None if code == "None" else code, name, text, fb)
                if got:
                    codes.add(got)
                    if by_fallback:
                        ok = got in gold
                        hits["right" if ok else "wrong"] += 1
                        rows.append(f"{'right' if ok else 'WRONG'}: {key} → {got} {alg.ENTRIES[got]['company_name_zh']} "
                                    f"[{rec['article']}]")
            tp += len(codes & set(gold))
            n_out += len(codes)
            n_gold += len(gold)
        p = tp / n_out if n_out else 1.0
        r = tp / n_gold if n_gold else 1.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        print(f"| {fb_name} | {p:.3f} | {r:.3f} | {f1:.3f} | {hits['right']} / {hits['wrong']} |")
        details[fb_name] = rows
    for fb_name in ("S8", "LLM spike subseq", "§4.2 as written"):
        print(f"\n{fb_name} fallback hits:")
        print("\n".join(f"- {row}" for row in details[fb_name]) or "- none")
    print()
