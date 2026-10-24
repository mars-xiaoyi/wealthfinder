## Per-case results at the default floor 0.6 (set intersection, as specified)

| Query | Category | Expected | Actual | Verdict | Tie at top? | Top candidates (overlap, lcs) |
|---|---|---|---|---|---|---|
| 騰訊 | abbreviation | 00700 騰訊控股 | 80700 騰訊控股－Ｒ | wrong | yes (3-way) | 80700 騰訊控股－Ｒ (2,2); 00700 騰訊控股 (2,2); 01698 騰訊音樂－ＳＷ (2,2) |
| 阿里 | abbreviation | 09988 阿里巴巴－Ｗ | 89988 阿里巴巴－ＷＲ | wrong | yes (3-way) | 89988 阿里巴巴－ＷＲ (2,2); 00241 阿里健康 (2,2); 09988 阿里巴巴－Ｗ (2,2) |
| 美團 | abbreviation | 03690 美團－Ｗ | 00300 美的集團 | wrong | yes (9-way) | 00300 美的集團 (2,2); 01850 美好生活集團控股 (2,2); 00335 美建集團 (2,2); 08371 嚐高美集團 (2,2) |
| 快手 | abbreviation | 01024 快手－Ｗ | 81024 快手－ＷＲ | wrong | yes (2-way) | 81024 快手－ＷＲ (2,2); 01024 快手－Ｗ (2,2) |
| 比亞迪 | abbreviation | 01211 比亞迪股份 | 81211 比亞迪股份－Ｒ | wrong | yes (3-way) | 81211 比亞迪股份－Ｒ (3,3); 01211 比亞迪股份 (3,3); 00285 比亞迪電子 (3,3) |
| 商湯 | abbreviation | 00020 商湯－Ｗ | 00020 商湯－Ｗ | correct | yes (2-way) | 00020 商湯－Ｗ (2,2); 80020 商湯－ＷＲ (2,2) |
| 中鋁 | abbreviation | 02600 中國鋁業 | 06898 中國鋁罐 | wrong | yes (3-way) | 06898 中國鋁罐 (2,2); 02600 中國鋁業 (2,2); 02068 中鋁國際 (2,2) |
| 農行 | abbreviation | 01288 農業銀行 | 03618 重慶農村商業銀行 | wrong | yes (4-way) | 03618 重慶農村商業銀行 (2,2); 01551 廣州農商銀行 (2,2); 09889 東莞農商銀行 (2,2); 01288 農業銀行 (2,2) |
| 恆地 | abbreviation | 00012 恒基地產 | — | missed |  |  |
| 潤地 | abbreviation | 01109 華潤置地 | 01109 華潤置地 | correct |  | 01109 華潤置地 (2,2) |
| 建板 | abbreviation | 01888 建滔積層板 | 01888 建滔積層板 | correct |  | 01888 建滔積層板 (2,2) |
| 民行 | abbreviation | 01988 民生銀行 | 01988 民生銀行 | correct |  | 01988 民生銀行 (2,2) |
| 建行 | abbreviation | 00939 建設銀行 | 00939 建設銀行 | correct |  | 00939 建設銀行 (2,2) |
| 中行 | abbreviation | 03988 中國銀行 | 03988 中國銀行 | correct | yes (4-way) | 03988 中國銀行 (2,2); 01216 中原銀行 (2,2); 06818 中國光大銀行 (2,2); 00998 中信銀行 (2,2) |
| 龍湖 | abbreviation | 00960 龍湖集團 | 00960 龍湖集團 | correct |  | 00960 龍湖集團 (2,2) |
| 中石油 | abbreviation | 00857 中國石油股份 | 01033 中石化油服 | wrong | yes (7-way) | 01033 中石化油服 (3,3); 00857 中國石油股份 (3,3); 80883 中國海洋石油－Ｒ (3,3); 00386 中國石油化工股份 (3,3) |
| 國航 | abbreviation | 00753 中國國航 | 01055 中國南方航空股份 | wrong | yes (5-way) | 01055 中國南方航空股份 (2,2); 00696 中國民航信息網絡 (2,2); 00293 國泰航空 (2,2); 00670 中國東方航空股份 (2,2) |
| 中銀 | abbreviation | 02388 中銀香港 | 82388 中銀香港－Ｒ | wrong | yes (10-way) | 82388 中銀香港－Ｒ (2,2); 00515 中華銀科技 (2,2); 02588 中銀航空租賃 (2,2); 03988 中國銀行 (2,2) |
| 小米 | abbreviation | 01810 小米集團－Ｗ | 01810 小米集團－Ｗ | correct | yes (2-way) | 01810 小米集團－Ｗ (2,2); 81810 小米集團－ＷＲ (2,2) |
| 滙豐 | abbreviation | 00005 匯豐控股 | 00727 中國豐滙 | wrong |  | 00727 中國豐滙 (2,1) |
| 京東 | abbreviation | 09618 京東集團－ＳＷ | 09618 京東集團－ＳＷ | correct | yes (7-way) | 09618 京東集團－ＳＷ (2,2); 02618 京東物流 (2,2); 06618 京東健康 (2,2); 86618 京東健康－Ｒ (2,2) |
| 中石化 | abbreviation | 00386 中國石油化工股份 | 01033 中石化油服 | wrong | yes (5-way) | 01033 中石化油服 (3,3); 02386 中石化煉化工程 (3,3); 00386 中國石油化工股份 (3,3); 00934 中石化冠德 (3,3) |
| 優地機器人 | full_name | 03231 優地機器人 | 03231 優地機器人 | correct |  | 03231 優地機器人 (5,5); 09660 地平線機器人－Ｗ (4,4); 01021 華沿機器人 (3,3); 01236 樂動機器人 (3,3) |
| 中國財險 | full_name | 02328 中國財險 | 02328 中國財險 | correct |  | 02328 中國財險 (4,4); 01508 中國再保險 (3,3); 01339 中國人民保險集團 (3,3) |
| 天數智芯 | full_name | 09903 天數智芯 | 09903 天數智芯 | correct |  | 09903 天數智芯 (4,4) |
| 順豐控股 | full_name | 06936 順豐控股 | 06936 順豐控股 | correct |  | 06936 順豐控股 (4,4); 00932 順騰國際控股 (3,3); 00005 匯豐控股 (3,3); 09882 永聯豐控股 (3,3) |
| 大新銀行 | full_name | 02356 大新銀行集團 | 02356 大新銀行集團 | correct |  | 02356 大新銀行集團 (4,4); 06818 中國光大銀行 (3,3) |
| 萬洲國際 | full_name | 00288 萬洲國際 | 00288 萬洲國際 | correct |  | 00288 萬洲國際 (4,4); 00898 萬事昌國際 (3,3); 02313 申洲國際 (3,3) |
| 麥科田 | full_name | 02041 麥科田 | 02041 麥科田 | correct |  | 02041 麥科田 (3,3); 02335 麥科醫藥－Ｂ (2,2) |
| 江波龍 | full_name | 09976 江波龍 | 09976 江波龍 | correct |  | 09976 江波龍 (3,3) |
| 納真科技 | full_name | 09856 納真科技 | 09856 納真科技 | correct |  | 09856 納真科技 (4,4); 08297 海納星空科技 (3,3); 01597 納泉能源科技 (3,3) |
| 復星國際 | full_name | 00656 復星國際 | 00656 復星國際 | correct |  | 00656 復星國際 (4,4) |
| 名創優品 | full_name | 09896 名創優品 | 09896 名創優品 | correct |  | 09896 名創優品 (4,4) |
| 智譜 | full_name | 02513 智譜 | 02513 智譜 | correct |  | 02513 智譜 (2,2) |
| MINIMAX | full_name | 00100 MINIMAX-W | 00100 MINIMAX-W | correct | yes (2-way) | 00100 MINIMAX-W (5,7); 00100 MINIMAX-W (5,7); 01418 SINOMAX GROUP (5,5); 02699 XINMING CHINA (5,5) |
| 中國銀行 | full_name | 03988 中國銀行 | 03988 中國銀行 | correct | yes (2-way) | 03988 中國銀行 (4,4); 06818 中國光大銀行 (4,4); 01216 中原銀行 (3,3); 06881 中國銀河 (3,3) |
| 香港交易所 | full_name | 00388 香港交易所 | 80388 香港交易所－Ｒ | wrong | yes (2-way) | 80388 香港交易所－Ｒ (5,5); 00388 香港交易所 (5,5) |
| Tencent | english | 00700 騰訊控股 | — | missed |  |  |
| Alibaba | english | 09988 阿里巴巴－Ｗ | — | missed |  |  |
| HSBC | english | 00005 匯豐控股 | 00005 匯豐控股 | correct | yes (2-way) | 00005 HSBC HOLDINGS (4,4); 08290 AHSAY BACKUP (4,4); 01931 ETHK LABS INC. (4,3); 01021 HUAYAN ROBOTICS (4,3) |
| Meituan | english | 03690 美團－Ｗ | 03690 美團－Ｗ | correct | yes (2-way) | 03690 MEITUAN-W (7,7); 83690 MEITUAN-WR (7,7); 01268 MEIDONG AUTO (7,4); 08100 FAMOUS TECH INT (7,4) |
| BYD | english | 01211 比亞迪股份 | 01906 博尼控股 | wrong | yes (12-way) | 01906 BONNY HLDG (3,3); 81211 BYD COMPANY-R (3,3); 02373 BEAUTYFARM MED (3,3); 01388 EMBRY HOLDINGS (3,3) |
| Xiaomi | english | 01810 小米集團－Ｗ | 01810 小米集團－Ｗ | correct | yes (2-way) | 01810 XIAOMI-W (5,6); 81810 XIAOMI-WR (5,6); 01418 SINOMAX GROUP (5,3); 08186 HONOR MATRIX (5,3) |
| AIA | english | 01299 友邦保險 | 00936 佳兆業資本 | wrong | yes (207-way) | 00936 KAISA CAPITAL (2,3); 08179 PALINDA GROUP (2,3); 00089 TAI SANG LAND (2,3); 00866 CHINA QINFA (2,3) |
| Ping An | english | 02318 中國平安 | 82318 中國平安－Ｒ | wrong | yes (3-way) | 82318 PING AN-R (5,6); 01809 PRINX CHENGSHAN (5,6); 02318 PING AN (5,6); 01736 PARENTING NET (5,5) |
| CATL | english | 03750 寧德時代 | 00936 佳兆業資本 | wrong | yes (43-way) | 00936 KAISA CAPITAL (4,4); 01629 CHAMP ALLI INTL (4,4); 08315 CEN PLAZA HOTEL (4,4); 00717 EMPEROR CAPITAL (4,4) |
| SenseTime | english | 00020 商湯－Ｗ | 00020 商湯－Ｗ | correct | yes (2-way) | 00020 SENSETIME-W (6,9); 80020 SENSETIME-WR (6,9); 02233 WESTCHINACEMENT (6,6); 00691 SHANSHUI CEMENT (6,6) |
| PetroChina | english | 00857 中國石油股份 | 00857 中國石油股份 | correct |  | 00857 PETROCHINA (10,10); 00144 CHINA MER PORT (10,5); 08156 SINOPHARM TECH (10,4); 03900 GREENTOWN CHINA (9,8) |
| Bank of China | english | 03988 中國銀行 | 03988 中國銀行 | correct |  | 03988 BANK OF CHINA (9,11); 06199 BANK OF GUIZHOU (8,7); 00888 BLOCKFIN HLDG (8,5); 06196 BANKOFZHENGZHOU (7,8) |
| Tencent Holdings | english | 00700 騰訊控股 | 00303 VTECH HOLDINGS | wrong | yes (3-way) | 00303 VTECH HOLDINGS (11,11); 00303 VTECH HOLDINGS (11,11); 08279 AGTECH HOLDINGS (11,11); 01793 WECON HOLDINGS (10,11) |
| HSBC Holdings plc | english | 00005 匯豐控股 | 00005 匯豐控股 | correct |  | 00005 HSBC HOLDINGS (10,12); 01742 HPC HOLDINGS (10,10); 01742 HPC HOLDINGS (10,10); 00002 CLP HOLDINGS (10,9) |
| 寧德時代 | a_h_dual | 03750 寧德時代 | 03750 寧德時代 | correct |  | 03750 寧德時代 (4,4) |
| 中國平安 | a_h_dual | 02318 中國平安 | 82318 中國平安－Ｒ | wrong | yes (2-way) | 82318 中國平安－Ｒ (4,4); 02318 中國平安 (4,4); 00966 中國太平 (3,3); 02399 中國安儲能源 (3,3) |
| 招商銀行 | a_h_dual | 03968 招商銀行 | 03968 招商銀行 | correct |  | 03968 招商銀行 (4,4); 01398 工商銀行 (3,3); 03618 重慶農村商業銀行 (3,3); 01551 廣州農商銀行 (3,3) |
| 中國神華 | a_h_dual | 01088 中國神華 | 01088 中國神華 | correct |  | 01088 中國神華 (4,4); 00485 中國華星 (3,3); 02779 中國新華教育 (3,3); 00377 中國華君 (3,3) |
| 比亞迪股份 | a_h_dual | 01211 比亞迪股份 | 81211 比亞迪股份－Ｒ | wrong | yes (2-way) | 81211 比亞迪股份－Ｒ (5,5); 01211 比亞迪股份 (5,5); 01244 思路迪醫藥股份 (3,3); 00285 比亞迪電子 (3,3) |
| 花旗 | non_hkex | — | — | correct |  |  |
| 美銀證券 | non_hkex | — | — | correct |  |  |
| 中銀國際 | non_hkex | — | 01822 中木國際 | wrong | yes (23-way) | 01822 中木國際 (3,3); 08516 中盈國際 (3,3); 08118 中資國際控股 (3,3); 02099 中國黃金國際 (3,3) |
| 野村 | non_hkex | — | — | correct |  |  |
| 巴克萊 | non_hkex | — | 04337 星巴克－Ｔ | wrong |  | 04337 星巴克－Ｔ (2,2); 00159 布萊克萬礦業 (2,1) |
| 富達 | non_hkex | — | 01348 滉達富控股 | wrong |  | 01348 滉達富控股 (2,1) |
| 海通國際 | non_hkex | — | 01579 頤海國際 | wrong | yes (8-way) | 01579 頤海國際 (3,3); 09658 特海國際 (3,3); 06123 圓通國際快遞 (3,3); 00248 香港通訊國際控股 (3,3) |
| 工銀亞洲 | non_hkex | — | — | correct |  |  |
| 58同城 | non_hkex | — | — | correct |  |  |
| 長鑫 | non_hkex | — | — | correct |  |  |
| 莫德納 | non_hkex | — | — | correct |  |  |
| 克而瑞 | non_hkex | — | — | correct |  |  |
| 標普 | non_hkex | — | — | correct |  |  |
| Anthropic | non_hkex | — | 00857 中國石油股份 | wrong | yes (2-way) | 00857 PETROCHINA (9,4); 08156 SINOPHARM TECH (9,4); 00144 CHINA MER PORT (9,3); 01021 HUAYAN ROBOTICS (8,6) |
| OpenAI | non_hkex | — | 02863 高豐集團控股 | wrong | yes (2-way) | 02863 GOLDEN FAITH GP (6,5); 01752 TOP EDUCATION (6,5); 00694 BEIJING AIRPORT (6,4); 00357 MEILAN AIRPORT (6,4) |
| Nvidia | non_hkex | — | 02260 環龍控股 | wrong | yes (4-way) | 02260 VANOV HOLDINGS (5,4); 00245 CHINA VERED FIN (5,4); 06031 SANY HEAVY IND (5,4); 08535 VISTAR HOLDINGS (5,4) |
| SB Energy | non_hkex | — | 01388 安莉芳控股 | wrong |  | 01388 EMBRY HOLDINGS (7,3); 01250 SDHS NEW ENERGY (6,7); 06656 SIGENERGY (6,7); 01251 SPT ENERGY (6,7) |
| 雲夢海餐飲 | non_hkex | — | — | correct |  |  |
| 平安 | ambiguous | 02318 中國平安 | 82318 中國平安－Ｒ | wrong | yes (3-way) | 82318 中國平安－Ｒ (2,2); 01833 平安好醫生 (2,2); 02318 中國平安 (2,2) |
| 地產 | ambiguous | — | 00089 大生地產 | wrong | yes (28-way) | 00089 大生地產 (2,2); 00016 新鴻基地產 (2,2); 00845 恒盛地產 (2,2); 00432 盈大地產 (2,2) |
| 銀行 | ambiguous | — | 01398 工商銀行 | wrong | yes (32-way) | 01398 工商銀行 (2,2); 06138 哈爾濱銀行 (2,2); 01916 江西銀行 (2,2); 03988 中國銀行 (2,2) |
| 內銀 | ambiguous | — | — | correct |  |  |
| 華潤 | ambiguous | — | 01109 華潤置地 | wrong | yes (11-way) | 01109 華潤置地 (2,2); 01209 華潤萬象生活 (2,2); 01515 華潤醫療 (2,2); 80291 華潤啤酒－Ｒ (2,2) |

## Totals by floor and intersection

| Floor | Intersection | Correct | Wrong | Missed | Cases whose outcome differs from set@0.6 |
|---|---|---|---|---|---|
| 0.5 | set | 36 | 43 | 0 | 恆地: — → 00089 大生地產<br>Tencent: — → 80700 騰訊控股－Ｒ<br>Alibaba: — → 09606 映恩生物－Ｂ<br>花旗: — → 00239 白花油<br>美銀證券: — → 08350 立橋證券控股<br>野村: — → 03618 重慶農村商業銀行<br>工銀亞洲: — → 01398 工商銀行<br>58同城: — → 09699 順豐同城<br>長鑫: — → 02182 天長集團<br>標普: — → 01913 普拉達<br>內銀: — → 01398 工商銀行 |
| 0.5 | Counter | 36 | 43 | 0 | 恆地: — → 00089 大生地產<br>Tencent: — → 80700 騰訊控股－Ｒ<br>Alibaba: — → 09887 維立志博－Ｂ<br>Tencent Holdings: 00303 VTECH HOLDINGS → 01793 偉工控股<br>花旗: — → 00239 白花油<br>美銀證券: — → 08350 立橋證券控股<br>野村: — → 03618 重慶農村商業銀行<br>工銀亞洲: — → 01398 工商銀行<br>58同城: — → 09699 順豐同城<br>長鑫: — → 02182 天長集團<br>標普: — → 01913 普拉達<br>Nvidia: 02260 環龍控股 → 00245 中薇金融<br>SB Energy: 01388 安莉芳控股 → 01250 山高新能源<br>內銀: — → 01398 工商銀行 |
| 0.6 | set | 44 | 32 | 3 |  |
| 0.6 | Counter | 44 | 34 | 1 | Tencent: — → 80700 騰訊控股－Ｒ<br>Alibaba: — → 09887 維立志博－Ｂ<br>Tencent Holdings: 00303 VTECH HOLDINGS → 01793 偉工控股<br>Nvidia: 02260 環龍控股 → 00245 中薇金融<br>SB Energy: 01388 安莉芳控股 → 01250 山高新能源 |
| 0.7 | set | 43 | 30 | 6 | AIA: 00936 佳兆業資本 → —<br>SenseTime: 00020 商湯－Ｗ → —<br>HSBC Holdings plc: 00005 匯豐控股 → —<br>巴克萊: 04337 星巴克－Ｔ → — |
| 0.7 | Counter | 45 | 33 | 1 | Tencent: — → 80700 騰訊控股－Ｒ<br>Alibaba: — → 09887 維立志博－Ｂ<br>Tencent Holdings: 00303 VTECH HOLDINGS → 01793 偉工控股<br>巴克萊: 04337 星巴克－Ｔ → —<br>Nvidia: 02260 環龍控股 → 00245 中薇金融<br>SB Energy: 01388 安莉芳控股 → 01250 山高新能源 |
| 0.8 | set | 44 | 27 | 8 | MINIMAX: 00100 MINIMAX-W → —<br>AIA: 00936 佳兆業資本 → —<br>SenseTime: 00020 商湯－Ｗ → —<br>Tencent Holdings: 00303 VTECH HOLDINGS → —<br>HSBC Holdings plc: 00005 匯豐控股 → —<br>中銀國際: 01822 中木國際 → —<br>巴克萊: 04337 星巴克－Ｔ → —<br>海通國際: 01579 頤海國際 → — |
| 0.8 | Counter | 47 | 30 | 2 | Tencent: — → 80700 騰訊控股－Ｒ<br>Alibaba: — → 09887 維立志博－Ｂ<br>Tencent Holdings: 00303 VTECH HOLDINGS → —<br>中銀國際: 01822 中木國際 → —<br>巴克萊: 04337 星巴克－Ｔ → —<br>海通國際: 01579 頤海國際 → —<br>Nvidia: 02260 環龍控股 → 00245 中薇金融<br>SB Energy: 01388 安莉芳控股 → 01250 山高新能源 |
| 1.0 | set | 44 | 24 | 11 | MINIMAX: 00100 MINIMAX-W → —<br>Xiaomi: 01810 小米集團－Ｗ → —<br>AIA: 00936 佳兆業資本 → —<br>Ping An: 82318 中國平安－Ｒ → —<br>SenseTime: 00020 商湯－Ｗ → —<br>Bank of China: 03988 中國銀行 → —<br>Tencent Holdings: 00303 VTECH HOLDINGS → —<br>HSBC Holdings plc: 00005 匯豐控股 → —<br>中銀國際: 01822 中木國際 → —<br>巴克萊: 04337 星巴克－Ｔ → —<br>海通國際: 01579 頤海國際 → —<br>Nvidia: 02260 環龍控股 → —<br>SB Energy: 01388 安莉芳控股 → — |
| 1.0 | Counter | 47 | 28 | 4 | Tencent: — → 80700 騰訊控股－Ｒ<br>Tencent Holdings: 00303 VTECH HOLDINGS → —<br>HSBC Holdings plc: 00005 匯豐控股 → —<br>中銀國際: 01822 中木國際 → —<br>巴克萊: 04337 星巴克－Ｔ → —<br>海通國際: 01579 頤海國際 → —<br>Nvidia: 02260 環龍控股 → 00245 中薇金融<br>SB Energy: 01388 安莉芳控股 → — |
