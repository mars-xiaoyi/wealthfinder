475 cases: dev 232, test 243 (positives 408, negatives 67)

| Matcher | dev correct / wrong / missed | dev utility | test correct / wrong / missed | test utility |
|---|---|---|---|---|
| spec §4.2 | 123 / 36 / 73 | 51 | 140 / 44 / 59 | 52 |
| revised R1–R7 | 144 / 11 / 77 | 122 | 165 / 9 / 69 | 147 |
| levenshtein (cutoff 0.8) | 126 / 0 / 106 | 126 | 136 / 0 / 107 | 136 |
| jaro_winkler (cutoff 0.8) | 194 / 24 / 14 | 146 | 204 / 22 / 17 | 160 |
| ratio (cutoff 90) | 126 / 0 / 106 | 126 | 136 / 0 / 107 | 136 |
| partial_ratio (cutoff 90) | 198 / 18 / 16 | 162 | 205 / 15 / 23 | 175 |
| WRatio (cutoff 90) | 194 / 12 / 26 | 170 | 201 / 10 / 32 | 181 |
| tfidf 1-2gram cosine (cutoff 0.6) | 189 / 13 / 30 | 163 | 198 / 14 / 31 | 170 |
| S0 exact only | 126 / 0 / 106 | 126 | 136 / 0 / 107 | 136 |
| S1 + legal-suffix strip | 145 / 0 / 87 | 145 | 149 / 0 / 94 | 149 |
| S2 + abbreviation = subsequence of name | 158 / 3 / 71 | 152 | 174 / 3 / 66 | 168 |
| S3 + long form: name is subsequence of query | 180 / 3 / 49 | 174 | 193 / 3 / 47 | 187 |
| S4 + tightness ranking | 183 / 4 / 45 | 175 | 194 / 5 / 44 | 184 |
| S5 + generic tail strip (控股/集團/Holdings) | 198 / 4 / 30 | 190 | 211 / 4 / 28 | 203 |
| S6 + Latin token overlap | 198 / 4 / 30 | 190 | 211 / 4 / 28 | 203 |
| S7 + Latin: name ⊆ query, prefix tokens | 213 / 7 / 12 | 199 | 217 / 6 / 20 | 205 |
| S8 + compactness before anchoring | 214 / 6 / 12 | 202 | 217 / 6 / 20 | 205 |
| S9 + tie → lowest stock code | 217 / 12 / 3 | 193 | 217 / 14 / 12 | 189 |
| S10 + long-form contiguity (final) | 214 / 6 / 12 | 202 | 216 / 8 / 19 | 200 |
