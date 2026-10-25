# Spike: `lookup_stock` Fuzzy Matching

Throwaway scripts that find the best fuzzy-matching algorithm for `lookup_stock`, measured against the live HK Stock List (2,798 equities, fetched 2026-10-24). No LLM is involved. Starting point: implementation.md §4.2's `_fuzzy_match` as written.

## Result in one table

475 labeled cases (details below), all scored as correct / wrong / missed. A wrong match is accepted silently, while a miss makes the FC loop retry with another name or the code, so wrong matches matter more.

| Matcher | Correct | Wrong | Missed |
|---|---|---|---|
| §4.2 as written | 263 | 80 | 132 |
| best library scorer (rapidfuzz `WRatio`, cutoff 90, tuned on dev) | 395 | 22 | 58 |
| **ordered-subsequence matcher (S8)** | **431** | **12** | **32** |

Per-category breakdown and latency: `results_compare.md`. Every round on the dev and test halves: `results_rounds.md`.

## Research: what the literature and libraries say

- **Chinese abbreviations are ordered subsequences of the full name** ([arXiv 1712.06289](https://ar5iv.arxiv.org/html/1712.06289)). Characters come from anywhere in the name, not just word starts. A baseline that always takes word-initial characters gets 25.77%. 農行 ⊂ 農業銀行, 新地 ⊂ 新鴻基地產 and 國壽 ⊂ 中國人壽 all fit; §4.2's character-count overlap ignores the order.
- **Edit-distance and token measures show no consistent winner on Chinese names** ([Peng, Yu & Dredze, ACL 2015](https://aclanthology.org/P15-2062.pdf)). They name Levenshtein, Jaro-Winkler and soft TF-IDF. Their main fix is normalization first, such as folding traditional and simplified forms. This spike measured all three families, and none of them beats a subsequence rule.
- **Alignment with a company's full name plus rules** is the approach in Chinese financial-entity abbreviation work ([Journal of Computer Applications 2016](https://www.joca.cn/EN/Y2016/V36/I2/449)). It segments the full name, finds core words, and drops legal-form words.
- **Legal-suffix stripping before comparing** is standard practice for company names ([cleanco](https://pypi.org/project/cleanco/2.1/)).
- **`rapidfuzz`** (`WRatio`, `partial_ratio`, `token_set_ratio`) is the usual tool for general string matching ([docs](https://github.com/rapidfuzz/RapidFuzz)). **Character n-gram TF-IDF + cosine** is the usual approach for name search ([overview](https://arxiv.org/pdf/2112.14030)). Both were measured as references below.

## Test set (`eval_cases.py`)

475 cases, every label coming from a real article or from HKEX's own list. They are split half and half by a hash of the query, into dev (tune on it) and test (report only).

| Source | Cases | Label |
|---|---|---|
| Ming Pao, AASTOCKS, Yahoo HK articles, mentions written as `name(code)` | 210 | the code the article itself printed |
| the same articles, foreign companies written as `name(TICKER.US)` or a mainland-only code | 42 + 3 | no HKEX listing, so a miss is correct (3 have a US-stock `－Ｔ` counter) |
| HKEX announcements: issuer's English and Chinese legal name in the header | 77 + 64 | the announcement's stock code |
| hand-collected from the first 85 articles (abbreviations, English, A/H, ambiguous, non-HKEX); queries that repeat another row are dropped | up to 79 | hand-checked |

Dated 2026-10-24, crawled with SADI's own crawlers into a local DB: 148 Ming Pao, 38 AASTOCKS, 5 Yahoo HK (Yahoo's RSS failed to parse today, so only the earlier 5 exist) and 192 HKEX announcements for 2026-10-22/23. Positives whose code is not on the equity list (a REIT, for example) are dropped. `data/` holds the case lists. The raw article exports (`data/*_articles.tsv`, 7 MB of third-party news text) are not committed; the export commands are in `extract_news.py` and `extract_hkex.py`, which rebuild the case lists from them.

## The 10 rounds (`subseq.py`, scored by `evaluate.py`)

Utility = correct − 2 × wrong. Dev numbers are what each round was judged on; test numbers were only looked at to confirm.

| Round | Change | Dev c / w / m | Test c / w / m |
|---|---|---|---|
| S0 | exact match after normalization (traditional→simplified via OpenCC, full-width→ASCII, HKEX name markers stripped, RMB counters and `－Ｔ` lines dropped) | 126 / 0 / 106 | 136 / 0 / 107 |
| S1 | strip legal suffixes (有限公司, Limited, Co., Ltd.) from both sides | 145 / 0 / 87 | 149 / 0 / 94 |
| S2 | the query is an ordered subsequence of a name (abbreviation direction) | 158 / 3 / 71 | 174 / 3 / 66 |
| S3 | a listed name is an ordered subsequence of a longer query (legal-name direction) | 180 / 3 / 49 | 193 / 3 / 47 |
| S4 | rank subsequence hits by anchoring on the first character, then compactness | 183 / 4 / 45 | 194 / 5 / 44 |
| S5 | also compare with generic tails stripped (控股, 集團, Holdings, Group) | 198 / 4 / 30 | 211 / 4 / 28 |
| S6 | Latin queries by token (kept only as a stepping stone) | 198 / 4 / 30 | 211 / 4 / 28 |
| S7 | Latin: the whole listed name inside the query, or the reverse, with prefix tokens (DEV ⊂ DEVELOPMENT) | 213 / 7 / 12 | 217 / 6 / 20 |
| **S8** | **compactness outranks first-character anchoring** | **214 / 6 / 12** | **217 / 6 / 20** |
| S9 | tie between companies → lowest stock code (**rejected**) | 217 / 12 / 3 | 217 / 14 / 12 |
| S10 | long-form hits ranked by contiguity; 2-character names allowed when contiguous (**rejected**) | 214 / 6 / 12 | 216 / 8 / 19 |

- S9 turns 9 misses into 3 right answers and 6 more wrong ones. Utility drops (202 → 193 dev, 205 → 189 test), so a tie stays a miss.
- S10 is flat on dev and adds 2 wrong matches on test, such as 埃克森美孚 → 森美控股.
- Library scorers were run for reference only, with cutoffs picked on dev: `levenshtein`/`ratio` never go wrong but miss about half; `jaro_winkler` and `partial_ratio` go wrong 18–24 times on dev; `WRatio` and TF-IDF are the best of them at 12–13 wrong on dev.

## Held-out check (`holdout.py`, `results_holdout.md`)

The 475-case set was fixed before the rounds, so S8 was also scored once, unchanged, on HKEX announcements from 2026-10-19 to 10-21, which no round ever saw. Companies already in the main set are excluded: 49 cases, 28 companies.

| Matcher | Correct | Wrong | Missed |
|---|---|---|---|
| §4.2 as written | 1 | 2 | 46 |
| `WRatio`, cutoff 90 | 34 | 1 | 14 |
| **S8** | **42** | **2** | **5** |

S8's rates match the in-sample HKEX rows (English legal names 20 of 26 right here, 57 of 77 before; Chinese legal names 22 of 23 here, 61 of 64 before). The held-out set has HKEX legal names only: no news mentions, no negatives. News articles can't be re-crawled for past days, so a held-out news set needs fresh articles on a later day.

Two of its three errors are worth knowing: "POSTAL SAVINGS BANK OF CHINA CO., LTD." → 中國銀行, because the whole listed name "BANK OF CHINA" appears inside the query; and 視頻有限公司 → 中國數字視頻, which is a truncated extraction of the issuer name (label noise).

## Held-out news check (`holdout_sep.py`, `results_holdout_sep.md`)

S8 was then scored once, unchanged, on cleaned articles published 2026-09-01 to 09-07 (`cleaned_news_export.csv`, 220 news articles and 160 HKEX announcements). Articles from 09-08 to 09-13 are reserved for implementation testing and were not read. Companies used by any earlier case set are excluded. That leaves 198 cases from 144 companies: 107 news `name(code)` mentions (`data/sep_news_cases.py`), 9 A-share/US-only companies and 82 HKEX legal names (`data/sep_hkex_cases.py`). The pass bar was fixed before scoring: S8 must get more right than `WRatio`, and S8's wrong rate must be 3% or below.

| Matcher | Correct | Wrong | Missed |
|---|---|---|---|
| §4.2 as written | 106 | 10 | 82 |
| `WRatio`, cutoff 90 | 180 | 1 | 17 |
| **S8** | **187** | **2** | **9** |

S8 passes (187 > 180; 1.0% wrong). On news mentions S8 and `WRatio` tie at 105 / 0 / 2 and both reject all 9 non-HKEX companies. The gap comes from HKEX English legal names (S8 35 right against 29). The two news misses are 群核 / 群核科技, whose listed Chinese name is the English "MANYCORE TECH". Both wrong matches are English legal names: "RUIFENG GREEN ENERGY AI COMPUTING HOLDINGS" → 綠色能源科技集團 and "CENTRAL CHINA MANAGEMENT" → 建業地產. This is the known short-name-inside-a-longer-name limit.

## What S8 does (final)

1. **Code path** unchanged from §4.2 (`\d{2,5}(\.HK)?`, zero-pad to 5).
2. **Load time, once per refresh:** drop RMB counters and `－Ｔ` trading-only lines; for each of a stock's two names build comparison forms: normalized (OpenCC `t2s`, full-width to ASCII, casefold, HKEX name marker stripped), legal suffix stripped, generic tail stripped. English forms keep word spaces. Build a small index from English word prefix to stocks.
3. **Query:** build the same forms. One stock matching a form exactly wins. Otherwise score every stock; the best score wins only if it is unique, and a tie is a miss:
   - CJK, the query is an ordered subsequence of the name: rank by compactness (query length ÷ span in the name), then first-character anchoring, then coverage, then the shorter name.
   - CJK, a name of 3+ characters is an ordered subsequence of the query: rank by coverage, then name length.
   - Latin: the whole listed name appears in the query, or the whole query appears in the name, with a word matching when equal or when the listed word is a ≥3-letter prefix of the query word.
4. A pre-filter skips names whose characters aren't nested with the query's.

Cost: p50 0.17 ms, p99 4.2 ms, max 6.5 ms over all 475 queries (Python 3.12, Apple M5 Pro). That is below the 20 ms warning threshold with names prepared at load. This also resolves the I-1 pre-traffic question: `asyncio.to_thread()` is not needed.

## What S8 still gets wrong or misses (`results_rounds.md`, `--failures S8`)

- **Ties between equally good companies** (about 12 misses): 恒地 / 新地 / 世茂 / 聯通 / 阿里 / 中行 / 中石油 / 中石化 each match several companies equally well. Characters can't decide, and the article almost always prints the code next to the name (`新地(00016.HK)`).
- **Mainland companies' English legal names** (about 16 HKEX misses, e.g. "Shandong Gold Mining Co., Ltd." → 山東黃金): the list's English short names are not translations of the Chinese name. Only the code or the Chinese name resolves these. HKEX announcements print the stock code in the header.
- **Short real names that are also parts of other names** (6 wrong): 德銀 → 德銀天下, 瑞銀 → 華泰瑞銀, 康寧 → 康寧醫院, Seagate → 爪哇控股, 中鋁 → 中鋁國際 (CHALCO's abbreviation vs a different company), 新地 → 新天地產集團.
- **Not measured: an alias table.** Abbreviations no rule can derive (中海油 = 中國海洋石油) could come from a small table seeded with the article-printed pairs, such as this spike's 210 `name(code)` mentions. It is the standard remedy, but it needs upkeep and can't be tested fairly on the data it was built from.

## Files

| File | What it does |
|---|---|
| `fetch_stock_list.py` | loads the live list with SAPI's own loader into `stock_list_snapshot.json` |
| `cases.py`, `data/news_cases.py`, `data/hkex_cases.py`, `eval_cases.py` | the case sets and the dev/test split |
| `extract_news.py`, `extract_hkex.py` | rebuild the case material from the SADI exports in `data/` |
| `match_cases.py`, `match_revised.py` | §4.2 as written, and the first-pass fixes (kept for comparison) |
| `algorithms.py` | normalization, `rapidfuzz` scorers and the TF-IDF matcher |
| `subseq.py` | the subsequence matcher, one flag per round |
| `evaluate.py`, `compare.py`, `holdout.py`, `holdout_sep.py` | results per round; per-category results and latency; the held-out HKEX and news checks |
| `bench.py` | the earlier I-1 latency and heartbeat test for §4.2 and R1–R7 |

`algorithms.py`, `subseq.py`, `evaluate.py` and `compare.py` need `rapidfuzz` and `opencc` (`uv pip install rapidfuzz opencc`); the rest run on a plain `python3`.
