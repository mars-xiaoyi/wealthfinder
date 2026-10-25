| Step | Correct | Wrong | Missed |
|---|---|---|---|
| baseline (§4.2 as written) | 44 | 32 | 3 |
| +R1 drop RMB counters | 50 | 26 | 3 |
| +R2 strip HKEX name markers | 50 | 26 | 3 |
| +R3 fold 恆/滙 variants | 51 | 26 | 2 |
| +R4 Counter intersection | 52 | 27 | 0 |
| +R5 tie-break: contiguous, then shorter name | 61 | 18 | 0 |
| +R6 Latin query: whole-query substring | 64 | 12 | 3 |
| +R7 remaining tie → miss | 65 | 4 | 10 |
all fixes @ floor 0.5: {'missed': 10, 'correct': 61, 'wrong': 8}
all fixes @ floor 0.6: {'missed': 10, 'correct': 65, 'wrong': 4}
all fixes @ floor 0.7: {'missed': 10, 'correct': 66, 'wrong': 3}
all fixes @ floor 0.8: {'missed': 10, 'correct': 66, 'wrong': 3}
all fixes @ floor 1.0: {'missed': 10, 'correct': 66, 'wrong': 3}

## Remaining failures with every fix (floor 0.6)

| Query | Category | Expected | Actual | Verdict |
|---|---|---|---|---|
| 騰訊 | abbreviation | 00700 騰訊控股 | — | missed |
| 阿里 | abbreviation | 09988 阿里巴巴－Ｗ | — | missed |
| 比亞迪 | abbreviation | 01211 比亞迪股份 | — | missed |
| 中鋁 | abbreviation | 02600 中國鋁業 | 02068 中鋁國際 | wrong |
| 恆地 | abbreviation | 00012 恒基地產 | — | missed |
| 中行 | abbreviation | 03988 中國銀行 | — | missed |
| 中石油 | abbreviation | 00857 中國石油股份 | 00632 中港石油 | wrong |
| 京東 | abbreviation | 09618 京東集團－ＳＷ | — | missed |
| 中石化 | abbreviation | 00386 中國石油化工股份 | — | missed |
| Alibaba | english | 09988 阿里巴巴－Ｗ | — | missed |
| Tencent Holdings | english | 00700 騰訊控股 | — | missed |
| HSBC Holdings plc | english | 00005 匯豐控股 | — | missed |
| 巴克萊 | non_hkex | — | 04337 星巴克－Ｔ | wrong |
| 富達 | non_hkex | — | 01348 滉達富控股 | wrong |
