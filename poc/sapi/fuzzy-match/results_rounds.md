475 cases: dev 255, test 220 (positives 408, negatives 67)

| Matcher | dev correct / wrong / missed | dev utility | test correct / wrong / missed | test utility |
|---|---|---|---|---|
| spec §4.2 | 138 / 42 / 75 | 54 | 125 / 38 / 57 | 49 |
| revised R1–R7 | 161 / 11 / 83 | 139 | 148 / 9 / 63 | 130 |
| levenshtein (cutoff 0.8) | 135 / 0 / 120 | 135 | 127 / 0 / 93 | 127 |
| jaro_winkler (cutoff 0.8) | 211 / 28 / 16 | 155 | 187 / 18 / 15 | 151 |
| ratio (cutoff 80) | 145 / 3 / 107 | 139 | 129 / 0 / 91 | 129 |
| partial_ratio (cutoff 90) | 212 / 22 / 21 | 168 | 191 / 11 / 18 | 169 |
| WRatio (cutoff 90) | 210 / 12 / 33 | 186 | 185 / 10 / 25 | 165 |
| tfidf 1-2gram cosine (cutoff 0.6) | 210 / 12 / 33 | 186 | 177 / 15 / 28 | 147 |
| S0 exact only | 135 / 0 / 120 | 135 | 127 / 0 / 93 | 127 |
| S1 + legal-suffix strip | 149 / 0 / 106 | 149 | 145 / 0 / 75 | 145 |
| S2 + abbreviation = subsequence of name | 170 / 4 / 81 | 162 | 162 / 2 / 56 | 158 |
| S3 + long form: name is subsequence of query | 195 / 4 / 56 | 187 | 178 / 2 / 40 | 174 |
| S4 + tightness ranking | 197 / 5 / 53 | 187 | 180 / 4 / 36 | 172 |
| S5 + generic tail strip (控股/集團/Holdings) | 212 / 4 / 39 | 204 | 197 / 4 / 19 | 189 |
| S6 + Latin token overlap | 212 / 4 / 39 | 204 | 197 / 4 / 19 | 189 |
| S7 + Latin: name ⊆ query, prefix tokens | 227 / 7 / 21 | 213 | 203 / 6 / 11 | 191 |
| S8 + compactness before anchoring | 228 / 6 / 21 | 216 | 203 / 6 / 11 | 191 |
| S9 + tie → lowest stock code | 230 / 16 / 9 | 198 | 204 / 10 / 6 | 184 |
| S10 + long-form contiguity (final) | 229 / 6 / 20 | 217 | 201 / 8 / 11 | 185 |
