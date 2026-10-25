198 held-out cases, 144 companies, news_hk 107, news_non_hk 9, hkex_legal_en 44, hkex_legal_zh 38

| Matcher | all c / w / m | news_hk c / w / m | news_non_hk c / w / m | hkex_legal_en c / w / m | hkex_legal_zh c / w / m |
|---|---|---|---|---|---|
| spec §4.2 | 106 / 10 / 82 | 94 / 4 / 9 | 8 / 1 / 0 | 0 / 3 / 41 | 4 / 2 / 32 |
| WRatio@90 | 180 / 1 / 17 | 105 / 0 / 2 | 9 / 0 / 0 | 29 / 1 / 14 | 37 / 0 / 1 |
| S8 (final) | 187 / 2 / 9 | 105 / 0 / 2 | 9 / 0 / 0 | 35 / 2 / 7 | 38 / 0 / 0 |

Pass bar: S8 correct 187 > WRatio correct 180: True; S8 wrong rate 1.0% ≤ 3%: True

S8 failures:
- missed: 群核 [news_hk] expected 00068 MANYCORE TECH, got —
- missed: 群核科技 [news_hk] expected 00068 MANYCORE TECH, got —
- missed: XINHUA NEWS MEDIA HOLDINGS LIMITED [hkex_legal_en] expected 00309 新華通訊頻媒, got —
- wrong: RUIFENG GREEN ENERGY AI COMPUTING HOLDINGS LIMITED [hkex_legal_en] expected 00527 瑞風綠能智算, got 00979 綠色能源科技集團
- missed: Standard Development Group Limited [hkex_legal_en] expected 01867 標準發展集團, got —
- missed: MODERN INNOVATIVE DIGITAL TECHNOLOGY COMPANY LIMITED [hkex_legal_en] expected 02322 新質數字, got —
- missed: NANJING SINOLIFE UNITED COMPANY LIMITED [hkex_legal_en] expected 03332 中生聯合, got —
- missed: Bank of Jiujiang Co., Ltd. [hkex_legal_en] expected 06190 九江銀行, got —
- missed: CHINA BOHAI BANK CO., LTD. [hkex_legal_en] expected 09668 渤海銀行, got —
- missed: Dongguan Rural Commercial Bank Co., Ltd. [hkex_legal_en] expected 09889 東莞農商銀行, got —
- wrong: CENTRAL CHINA MANAGEMENT COMPANY LIMITED [hkex_legal_en] expected 09982 中原建業, got 00832 建業地產
