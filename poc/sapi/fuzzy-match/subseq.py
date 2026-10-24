"""Ordered-subsequence matcher, built up in rounds (each round turns on one more feature via flags).

Chinese abbreviations are ordered subsequences of the full name (農行 ⊂ 農業銀行), and a listed short name is an
ordered subsequence of the issuer's legal name (赤峰黃金 ⊂ 赤峰吉隆黃金礦業集團), so both directions are tried.
"""

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

import algorithms as alg
from match_cases import ENTRIES

_ZH_LEGAL = re.compile(r"(股份)?有限公司$|公司$")
_EN_LEGAL_WORDS = {"limited", "ltd", "co", "company", "corporation", "corp", "inc", "plc", "llc", "the"}
_EN_GENERIC = {"holdings", "holding", "hldg", "hldgs", "group", "gp", "international", "intl", "int'l", "and", "&"}
_ZH_GENERIC_TAIL = re.compile(r"(控股|集團|國際|股份|實業)+$")


@dataclass
class Flags:
    legal_suffix: bool = False      # R1: strip 有限公司 / Limited from both sides
    abbrev_subseq: bool = False     # R2: query is an ordered subsequence of a name
    long_form: bool = False         # R3: a name is an ordered subsequence of a (long) query
    tail_generic: bool = False      # R4: also compare with 控股/集團/Holdings/Group stripped
    tightness: bool = False         # R5: rank subsequence hits by anchoring and span
    latin_tokens: bool = False      # R6: Latin queries match by token overlap, not characters
    latin_both: bool = False        # R7: Latin name tokens ⊆ query tokens too, with prefix tolerance (DEV ⊂ DEVELOPMENT)
    compact_first: bool = False     # R8: span compactness outranks first-character anchoring
    long_contig: bool = False       # R10: long-form hits rank by contiguity; 2-char names allowed when contiguous
    code_prior: bool = False        # R9: a tie between companies goes to the lowest stock code
    tie_miss: bool = True           # unresolved tie between companies → miss
    min_q: int = 2


@dataclass
class Entry:
    code: str
    raw: str
    forms: list[str] = field(default_factory=list)   # normalized comparison forms, longest first
    latin: bool = False


def strip_legal(text: str, latin: bool) -> str:
    if latin:
        toks = [t for t in re.split(r"[\s.,]+", text) if t and t not in _EN_LEGAL_WORDS]
        return " ".join(toks)
    return _ZH_LEGAL.sub("", text)


def strip_generic(text: str, latin: bool) -> str:
    if latin:
        return " ".join(t for t in text.split() if t not in _EN_GENERIC)
    return _ZH_GENERIC_TAIL.sub("", text)


_SPACE_BETWEEN_LATIN = re.compile(r"(?<=[a-z0-9])\s+(?=[a-z0-9])")


def normalize(text: str) -> str:
    """Like alg.normalize, but keeps one space between Latin words so English names can be compared by token."""
    from_alg = alg._MARKER_RE.sub("", text).translate(alg._FULLWIDTH)
    from_alg = alg._T2S.convert(from_alg).casefold()
    from_alg = re.sub(r"\s+", " ", from_alg).strip()
    # drop spaces unless both neighbours are Latin letters/digits
    return _SPACE_BETWEEN_LATIN.sub("\x00", from_alg).replace(" ", "").replace("\x00", " ")


def forms_of(raw: str, f: Flags) -> list[str]:
    n = normalize(raw)
    latin = n.isascii()
    out = [n]
    if f.legal_suffix:
        out.append(strip_legal(n, latin))
    if f.tail_generic:
        out.append(strip_generic(strip_legal(n, latin), latin))
    return list(dict.fromkeys(x for x in out if x))


def subseq_span(q: str, name: str) -> tuple[int, int] | None:
    """Earliest-ending ordered match of q in name as (start, end) exclusive; greedy leftmost, then tightened."""
    i = 0
    first = None
    for pos, ch in enumerate(name):
        if i < len(q) and ch == q[i]:
            if i == 0:
                first = pos
            i += 1
            if i == len(q):
                end = pos + 1
                break
    else:
        return None
    # tighten: walk back from the end to find the latest start for this end
    j = len(q) - 1
    k = end - 1
    while j >= 0:
        if name[k] == q[j]:
            j -= 1
        k -= 1
    return k + 1, end


class Matcher:
    def __init__(self, flags: Flags):
        self.f = flags
        self.entries: list[Entry] = []
        for code, e in ENTRIES.items():
            if alg.is_rmb_counter(code, e) or alg.is_trading_only(e):
                continue
            for raw in {e["company_name_zh"], e["company_name_en"]}:
                forms = forms_of(raw, flags)
                if forms:
                    self.entries.append(Entry(code, raw, forms, forms[0].isascii()))
        self.latin_index: dict[str, list[Entry]] = {}   # first 3 letters of a name token → entries having it
        for e in self.entries:
            if e.latin:
                for tok in {t[:3] for fm in e.forms for t in fm.split() if len(t) >= 3}:
                    self.latin_index.setdefault(tok, []).append(e)

    def __call__(self, query: str) -> str | None:
        is_code, code = alg.code_lookup(query)
        if is_code:
            return code
        f = self.f
        forms = forms_of(query, f)
        if not forms:
            return None
        q_full = forms[0]
        q_latin = q_full.isascii()

        # exact normalized equality on any form
        exact = {e.code for e in self.entries for fm in e.forms if fm in forms}
        if len(exact) == 1:
            return next(iter(exact))
        if len(exact) > 1:
            return None if f.tie_miss else sorted(exact)[0]

        scored: list[tuple[tuple, str]] = []
        if q_latin:
            pool = {id(e): e for fm in forms for t in fm.split() if len(t) >= 3
                    for e in self.latin_index.get(t[:3], ())}.values()
        else:
            pool = [e for e in self.entries if not e.latin]
        for e in pool:
            best = None
            for q in forms:
                for name in e.forms:
                    key = self._score(q, name, q_latin, e.latin)
                    if key is not None and (best is None or key > best):
                        best = key
            if best is not None:
                scored.append((best, e.code))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        top_key = scored[0][0]
        tied = {c for k, c in scored if k == top_key}
        if len(tied) > 1:
            if f.code_prior:
                return sorted(tied)[0]
            return None if f.tie_miss else sorted(tied)[0]
        return scored[0][1]

    def _score(self, q: str, name: str, q_latin: bool, name_latin: bool) -> tuple | None:
        f = self.f
        if q_latin != name_latin and not f.abbrev_subseq:
            return None
        if q_latin and name_latin:
            if f.latin_tokens:
                qt, nt = q.split(), name.split()
                if not qt or not nt:
                    return None
                if f.latin_both:
                    hit_q = sum(any(n == t or (len(t) >= 3 and n.startswith(t)) for n in nt) for t in qt)
                    hit_n = sum(any(t == n or (len(n) >= 3 and t.startswith(n)) for t in qt) for n in nt)
                    if hit_n == len(nt) and len(nt) >= 1 and sum(len(n) for n in nt) >= 3:
                        return (4, hit_n / len(qt), len(nt))          # the whole name appears in the query
                    if hit_q == len(qt):
                        return (3, hit_q / len(nt), -len(name))       # the whole query appears in the name
                    return None
                if not set(qt) <= set(nt):
                    return None
                return (3, len(qt) / len(nt), -len(name))
            return None
        if q_latin != name_latin:
            return None
        # CJK
        if len(q) < f.min_q:
            return None
        if q == name:
            return (9, 1.0, 0)
        q_set, n_set = set(q), set(name)
        if not (q_set <= n_set or n_set <= q_set):
            return None  # a subsequence in either direction needs nested character sets
        if f.abbrev_subseq and len(q) <= len(name):
            span = subseq_span(q, name)
            if span:
                start, end = span
                if f.tightness:
                    anchored = int(start == 0)
                    compact = len(q) / (end - start)
                    if f.compact_first:
                        return (5, compact, anchored, len(q) / len(name), -len(name))
                    return (5, anchored, compact, len(q) / len(name), -len(name))
                return (5, len(q) / len(name), -len(name))
        if f.long_form and len(name) >= 2 and len(name) < len(q):
            span = subseq_span(name, q)
            if span:
                start, end = span
                if f.long_contig:
                    compact = len(name) / (end - start)
                    if len(name) < 3 and compact < 1:
                        return None
                    return (4, compact, len(name) / len(q), len(name))
                if len(name) >= 3:
                    return (4, len(name) / len(q), len(name))
        return None


BASE = dict(legal_suffix=True, abbrev_subseq=True, long_form=True, tightness=True, tail_generic=True)

ROUNDS: list[tuple[str, Flags]] = [
    ("S0 exact only", Flags()),
    ("S1 + legal-suffix strip", Flags(legal_suffix=True)),
    ("S2 + abbreviation = subsequence of name", Flags(legal_suffix=True, abbrev_subseq=True)),
    ("S3 + long form: name is subsequence of query", Flags(legal_suffix=True, abbrev_subseq=True, long_form=True)),
    ("S4 + tightness ranking", Flags(legal_suffix=True, abbrev_subseq=True, long_form=True, tightness=True)),
    ("S5 + generic tail strip (控股/集團/Holdings)",
     Flags(legal_suffix=True, abbrev_subseq=True, long_form=True, tightness=True, tail_generic=True)),
    ("S6 + Latin token overlap",
     Flags(legal_suffix=True, abbrev_subseq=True, long_form=True, tightness=True, tail_generic=True,
           latin_tokens=True)),
    ("S7 + Latin: name ⊆ query, prefix tokens", Flags(**BASE, latin_tokens=True, latin_both=True)),
    ("S8 + compactness before anchoring", Flags(**BASE, latin_tokens=True, latin_both=True, compact_first=True)),
    ("S9 + tie → lowest stock code",
     Flags(**BASE, latin_tokens=True, latin_both=True, compact_first=True, code_prior=True)),
    ("S10 + long-form contiguity (final)", Flags(**BASE, latin_tokens=True, latin_both=True, compact_first=True,
                                                 long_contig=True)),
]
