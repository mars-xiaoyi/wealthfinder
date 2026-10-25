## 127 cleaned-news articles (C3_lite31_vf)

| Step-1 normalization | P | R | F1 |
|---|---|---|---|
| LLM spike's (impl.md §8.2 as written) | 0.948 | 0.976 | 0.962 |
| S8's | 0.948 | 0.976 | 0.962 |

Step-1 decisions that differ (code on the list, names compared):
- 2097|MIXUE Group 蜜雪冰城股份有限公司 vs 蜜雪集團: only S8's accepts (in gold)
- 1747|Home Control International Limited vs HOME CONTROL: only S8's accepts (not in gold)
- 8147|雲晶數智科技集團有限公司 vs 雲晶數智: only LLM spike's accepts (in gold)
- 3368|百盛商業集團 vs 百盛集團: only S8's accepts (in gold)

## no-code worst case (C3_lite31_nc)

| Step-1 normalization | P | R | F1 |
|---|---|---|---|
| LLM spike's (impl.md §8.2 as written) | 0.982 | 0.845 | 0.908 |
| S8's | 0.982 | 0.845 | 0.908 |

Step-1 decisions that differ (code on the list, names compared):
- 06189|廣東愛得威建設(集團)股份有限公司 vs 愛得威建設集團: only S8's accepts (in gold)
- 08371|嚐‧高美 vs 嚐高美集團: only LLM spike's accepts (in gold)
- 00012|恆地 vs 恒基地產: only S8's accepts (in gold)

