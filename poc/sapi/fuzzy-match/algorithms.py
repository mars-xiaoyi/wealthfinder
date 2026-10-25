"""Candidate matchers, each built once over the snapshot and called per query: match(query) -> code | None.

Run with the scratch venv that has rapidfuzz + opencc (see README).
"""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import opencc
from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler, Levenshtein

from match_cases import ENTRIES, _CODE_RE, lookup_stock
from match_revised import lookup_indexed

_T2S = opencc.OpenCC("t2s")
_MARKER_RE = re.compile(r"[－\-][A-ZＡ-Ｚ]{1,3}$")
_FULLWIDTH = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)})


def is_rmb_counter(code: str, entry: dict) -> bool:
    return code.startswith("8") and entry["company_name_zh"].endswith("Ｒ")


def is_trading_only(entry: dict) -> bool:
    return entry["company_name_zh"].endswith("Ｔ") or entry["company_name_en"].endswith("-T")


def normalize(text: str, *, t2s: bool = True, strip_markers: bool = True) -> str:
    if strip_markers:
        text = _MARKER_RE.sub("", text)
    text = text.translate(_FULLWIDTH)
    if t2s:
        text = _T2S.convert(text)
    return "".join(text.split()).casefold()


def code_lookup(query: str) -> tuple[bool, str | None]:
    text = query.strip()
    if _CODE_RE.fullmatch(text):
        code = text.upper().removesuffix(".HK").zfill(5)
        return True, (code if code in ENTRIES else None)
    return False, None


@dataclass
class Name:
    code: str
    text: str     # normalized
    latin: bool


def build_names(*, drop_rmb=True, drop_trading_only=True, **norm) -> list[Name]:
    names = []
    for code, entry in ENTRIES.items():
        if (drop_rmb and is_rmb_counter(code, entry)) or (drop_trading_only and is_trading_only(entry)):
            continue
        for raw in {entry["company_name_zh"], entry["company_name_en"]}:
            text = normalize(raw, **norm)
            if text:
                names.append(Name(code, text, text.isascii()))
    return names


# ── Baselines ───────────────────────────────────────────────────────────────────────────────────────
def spec(query: str) -> str | None:
    return lookup_stock(query, 0.6)


def revised_r1_r7(query: str) -> str | None:
    return lookup_indexed(query)


class LibraryScorer:
    """process.extractOne over every normalized name with a well-known scorer and a score cutoff — the
    standard way these libraries are used for name lookup."""

    def __init__(self, scorer, cutoff: float):
        self.names = build_names()
        self.texts = [n.text for n in self.names]
        self.scorer, self.cutoff = scorer, cutoff

    def __call__(self, query: str) -> str | None:
        is_code, code = code_lookup(query)
        if is_code:
            return code
        q = normalize(query)
        if not q:
            return None
        best = process.extractOne(q, self.texts, scorer=self.scorer, score_cutoff=self.cutoff)
        return self.names[best[2]].code if best else None


SCORERS = {
    "levenshtein": Levenshtein.normalized_similarity,
    "jaro_winkler": JaroWinkler.normalized_similarity,
    "ratio": fuzz.ratio,
    "partial_ratio": fuzz.partial_ratio,
    "WRatio": fuzz.WRatio,
}


class TfidfCosine:
    """Character 1–2-gram TF-IDF vectors, cosine similarity, best name above a cutoff."""

    def __init__(self, cutoff: float):
        self.names = build_names()
        self.cutoff = cutoff
        grams = [self._grams(n.text) for n in self.names]
        df = Counter(g for gs in grams for g in set(gs))
        n = len(self.names)
        self.idf = {g: math.log(n / d) + 1 for g, d in df.items()}
        self.vecs = [self._vec(gs) for gs in grams]
        self.postings = defaultdict(set)
        for i, gs in enumerate(grams):
            for g in gs:
                self.postings[g].add(i)

    @staticmethod
    def _grams(text: str) -> list[str]:
        return list(text) + [text[i:i + 2] for i in range(len(text) - 1)]

    def _vec(self, grams: list[str]) -> dict[str, float]:
        tf = Counter(grams)
        v = {g: c * self.idf.get(g, 0.0) for g, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {g: x / norm for g, x in v.items()}

    def __call__(self, query: str) -> str | None:
        is_code, code = code_lookup(query)
        if is_code:
            return code
        q = normalize(query)
        if not q:
            return None
        qv = self._vec(self._grams(q))
        cand = set().union(*(self.postings.get(g, set()) for g in qv)) if qv else set()
        best, best_i = 0.0, None
        for i in cand:
            s = sum(w * self.vecs[i].get(g, 0.0) for g, w in qv.items())
            if s > best:
                best, best_i = s, i
        return self.names[best_i].code if best_i is not None and best >= self.cutoff else None
