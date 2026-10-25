"""The lookup_stock fuzzy-matching spike's name matching (poc/sapi/fuzzy-match), imported at runtime, for
reference_entity_analysis.py: its company-name forms (implementation.md §4.2 name_forms), its S8 name search (§4.2
search, step 3 of verification) and its HK Stock List snapshot. Nothing of it is copied here. Needs opencc and
rapidfuzz.
"""

import os
import sys
from pathlib import Path

FUZZY_DIR = Path(__file__).resolve().parent.parent / "fuzzy-match"

sys.path.insert(0, str(FUZZY_DIR))
_cwd = os.getcwd()
os.chdir(FUZZY_DIR)   # its modules open stock_list_snapshot.json relative to themselves
try:
    import match_cases
    import subseq
finally:
    os.chdir(_cwd)

S8_FLAGS = dict(subseq.ROUNDS)["S8 + compactness before anchoring"]
S8 = subseq.Matcher(S8_FLAGS)
ENTRIES: dict[str, dict] = match_cases.ENTRIES   # HK Stock List snapshot, 2026-10-24


def name_forms(raw: str) -> list[str]:
    return subseq.forms_of(raw, S8_FLAGS)
