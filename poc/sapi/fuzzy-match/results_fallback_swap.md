## 127 cleaned-news articles (C3_lite31_vf, 127 scored articles)

| Fallback | P | R | F1 | fallback hits right / wrong |
|---|---|---|---|---|
| none | 0.953 | 0.970 | 0.962 | 0 / 0 |
| LLM spike subseq | 0.943 | 0.976 | 0.959 | 1 / 2 |
| §4.2 as written | 0.932 | 0.976 | 0.954 | 1 / 19 |
| WRatio@90 | 0.948 | 0.976 | 0.962 | 1 / 1 |
| S8 | 0.948 | 0.976 | 0.962 | 1 / 1 |

S8 fallback hits:
- WRONG: None|納真科技 → 09856 納真科技 [982d3f5b]
- right: 603288|海天味業 → 03288 海天味業 [f7c23612]

LLM spike subseq fallback hits:
- WRONG: 08603|瑞銀集團 → 08006 華泰瑞銀 [67085136]
- WRONG: None|納真科技 → 09856 納真科技 [982d3f5b]
- right: 603288|海天味業 → 03288 海天味業 [f7c23612]

§4.2 as written fallback hits:
- WRONG: 11070|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 11121|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 26453|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 26649|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 27328|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 27329|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 28183|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 49518|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 49986|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 49988|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 49991|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 55829|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 55869|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 60285|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 64178|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 67877|SG Issuer → 00177 江蘇寧滬高速公路 [3063a65e]
- WRONG: 08603|瑞銀集團 → 06639 瑞爾集團 [67085136]
- WRONG: None|納真科技 → 09856 納真科技 [982d3f5b]
- right: 603288|海天味業 → 03288 海天味業 [f7c23612]
- WRONG: 01383|太陽城集團 → 06811 太興集團 [fc3629fb]

## no-code worst case (C3_lite31_nc, 31 scored articles)

| Fallback | P | R | F1 | fallback hits right / wrong |
|---|---|---|---|---|
| none | 0.978 | 0.705 | 0.820 | 0 / 0 |
| LLM spike subseq | 0.982 | 0.837 | 0.904 | 17 / 0 |
| §4.2 as written | 0.955 | 0.829 | 0.888 | 16 / 3 |
| WRatio@90 | 0.973 | 0.837 | 0.900 | 17 / 1 |
| S8 | 0.982 | 0.845 | 0.908 | 18 / 0 |

S8 fallback hits:
- right: 06189|廣東愛得威建設(集團)股份有限公司 → 06189 愛得威建設集團 [hkex06189]
- right: None|中華銀科技 → 00515 中華銀科技 [news11]
- right: 00203|富智康集團 → 02038 富智康集團 [news161]
- right: 03939|中創新航 → 03931 中創新航 [news161]
- right: 09660|名創優品 → 09896 名創優品 [news161]
- right: 09922|泡泡瑪特 → 09992 泡泡瑪特 [news161]
- right: 00231|蒙牛乳業 → 02319 蒙牛乳業 [news161]
- right: 00578|中國宏橋 → 01378 中國宏橋 [news161]
- right: 03918|恒大物業 → 06666 恒大物業 [news161]
- right: 00196|偉祿科技 → 01196 偉祿科技股份 [news161]
- right: 09880|百度-SW → 09888 百度集團－Ｗ [news20]
- right: 02099|靈寶黃金 → 03330 靈寶黃金 [news20]
- right: 06188|老鋪黃金 → 06181 老鋪黃金 [news20]
- right: 09696|名創優品 → 09896 名創優品 [news20]
- right: 00001|長實集團 → 01113 長實集團 [news6]
- right: 00045|嘉華國際 → 00173 嘉華國際 [news6]
- right: 00014|九龍建業 → 00034 九龍建業 [news6]
- right: 00037|遠東發展 → 00035 遠東發展 [news6]

LLM spike subseq fallback hits:
- right: None|中華銀科技 → 00515 中華銀科技 [news11]
- right: 00203|富智康集團 → 02038 富智康集團 [news161]
- right: 03939|中創新航 → 03931 中創新航 [news161]
- right: 09660|名創優品 → 09896 名創優品 [news161]
- right: 09922|泡泡瑪特 → 09992 泡泡瑪特 [news161]
- right: 00231|蒙牛乳業 → 02319 蒙牛乳業 [news161]
- right: 00578|中國宏橋 → 01378 中國宏橋 [news161]
- right: 03918|恒大物業 → 06666 恒大物業 [news161]
- right: 00196|偉祿科技 → 01196 偉祿科技股份 [news161]
- right: 09880|百度-SW → 09888 百度集團－Ｗ [news20]
- right: 02099|靈寶黃金 → 03330 靈寶黃金 [news20]
- right: 06188|老鋪黃金 → 06181 老鋪黃金 [news20]
- right: 09696|名創優品 → 09896 名創優品 [news20]
- right: 00001|長實集團 → 01113 長實集團 [news6]
- right: 00045|嘉華國際 → 00173 嘉華國際 [news6]
- right: 00014|九龍建業 → 00034 九龍建業 [news6]
- right: 00037|遠東發展 → 00035 遠東發展 [news6]

§4.2 as written fallback hits:
- right: None|中華銀科技 → 00515 中華銀科技 [news11]
- right: 00203|富智康集團 → 02038 富智康集團 [news161]
- right: 03939|中創新航 → 03931 中創新航 [news161]
- right: 09660|名創優品 → 09896 名創優品 [news161]
- right: 09922|泡泡瑪特 → 09992 泡泡瑪特 [news161]
- right: 00231|蒙牛乳業 → 02319 蒙牛乳業 [news161]
- right: 00578|中國宏橋 → 01378 中國宏橋 [news161]
- right: 03918|恒大物業 → 06666 恒大物業 [news161]
- right: 00196|偉祿科技 → 01196 偉祿科技股份 [news161]
- WRONG: 09880|百度-SW → 09618 京東集團－ＳＷ [news20]
- WRONG: 00016|恒地 → 00845 恒盛地產 [news20]
- right: 02099|靈寶黃金 → 03330 靈寶黃金 [news20]
- right: 06188|老鋪黃金 → 06181 老鋪黃金 [news20]
- right: 09696|名創優品 → 09896 名創優品 [news20]
- right: 00001|長實集團 → 01113 長實集團 [news6]
- right: 00045|嘉華國際 → 00173 嘉華國際 [news6]
- right: 00014|九龍建業 → 00034 九龍建業 [news6]
- right: 00037|遠東發展 → 00035 遠東發展 [news6]
- WRONG: 00778|置富 → 00225 博富臨置業 [news6]

