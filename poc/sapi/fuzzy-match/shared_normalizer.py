"""Step 1 of EntityAnalysisSkill's verification (`_name_consistent`, impl.md §8.2) with S8's normalization instead of
its own, on the Entity Analysis LLM Skill spike's stored C3 outputs, S8 as the step-3 name search in both runs.

S8's normalization: OpenCC t2s, full-width to ASCII, HKEX name markers stripped, legal suffixes and generic tails
stripped (subseq.forms_of). The agreement rule is unchanged: one form is an ordered subsequence of the other, the
listed name needing at least 60% of the mention's length in the reverse direction.

    <scratch venv>/bin/python shared_normalizer.py <llm-skill spike dir>
"""

import io
import sys
from contextlib import redirect_stdout

with redirect_stdout(io.StringIO()):
    import fallback_swap as fs  # its own report is not needed here
import subseq

S8 = dict(subseq.ROUNDS)["S8 + compactness before anchoring"]


def _is_subseq(a: str, b: str) -> bool:
    it = iter(b)
    return bool(a) and all(ch in it for ch in a)


def consistent_s8(name: str, entry: dict) -> bool:
    query_forms = subseq.forms_of(name, S8)
    for cand in (entry["company_name_zh"], entry["company_name_en"]):
        for n in subseq.forms_of(cand, S8) if cand else []:
            for q in query_forms:
                if _is_subseq(q, n) or (len(n) >= 0.6 * len(q) and _is_subseq(n, q)):
                    return True
    return False


spike_check = fs.harness["name_compatible"]
for label, run, eval_set in (("127 cleaned-news articles", "C3_lite31_vf", "eval_set_verify.json"),
                             ("no-code worst case", "C3_lite31_nc", "eval_set_nocode.json")):
    data = fs.load(run, eval_set)
    print(f"## {label} ({run})\n")
    print("| Step-1 normalization | P | R | F1 |")
    print("|---|---|---|---|")
    for check_name, check in (("LLM spike's (impl.md §8.2 as written)", spike_check), ("S8's", consistent_s8)):
        fs.harness["name_compatible"] = check
        tp = n_out = n_gold = 0
        for rec, gold, text in data:
            codes = set()
            for _, key, _ in rec["lookups"]:
                code, name = key.split("|", 1)
                got, _ = fs.verify(None if code == "None" else code, name, text, fs.FALLBACKS["S8"])
                if got:
                    codes.add(got)
            tp += len(codes & set(gold))
            n_out += len(codes)
            n_gold += len(gold)
        p, r = tp / n_out, tp / n_gold
        print(f"| {check_name} | {p:.3f} | {r:.3f} | {2 * p * r / (p + r):.3f} |")
    fs.harness["name_compatible"] = spike_check

    print("\nStep-1 decisions that differ (code on the list, names compared):")
    for rec, gold, _ in data:
        for _, key, _ in rec["lookups"]:
            code, name = key.split("|", 1)
            digits = (code if code != "None" else "").strip().upper().removesuffix(".HK")
            entry = fs.alg.ENTRIES.get(digits.zfill(5)) if digits.isdigit() else None
            if entry and spike_check(name, entry) != consistent_s8(name, entry):
                who = "LLM spike's" if spike_check(name, entry) else "S8's"
                in_gold = "in gold" if entry["stock_code"] in gold else "not in gold"
                print(f"- {key} vs {entry['company_name_zh']}: only {who} accepts ({in_gold})")
    print()
