"""Hand-collected lookup_stock queries from the 85 real SADI articles in the local volume-test DB
(AASTOCKS / Ming Pao / Yahoo HK, 2026-08-28..31), plus English and A/H names for the same companies.

Each case: (query, expected native code or None for "should miss", category, where it came from).
"""

ABBREV = "abbreviation"
FULL = "full_name"
ENGLISH = "english"
DUAL = "a_h_dual"
MISS = "non_hkex"
AMBIG = "ambiguous"

CASES: list[tuple[str, str | None, str, str]] = [
    # Abbreviations / short names as written in the articles
    ("騰訊", "00700", ABBREV, "AASTOCKS 花旗:騰訊(00700.HK)"),
    ("阿里", "09988", ABBREV, "Ming Pao 騰訊阿里資本開支"),
    ("美團", "03690", ABBREV, "AASTOCKS 花旗:美團(03690.HK)"),
    ("快手", "01024", ABBREV, "AASTOCKS 快手(01024.HK)"),
    ("比亞迪", "01211", ABBREV, "AASTOCKS 比亞迪(01211.HK)"),
    ("商湯", "00020", ABBREV, "AASTOCKS 商湯(00020.HK)"),
    ("中鋁", "02600", ABBREV, "AASTOCKS 中銀國際升中鋁(02600.HK)"),
    ("農行", "01288", ABBREV, "AASTOCKS 農行(01288.HK)"),
    ("恆地", "00012", ABBREV, "AASTOCKS 恆地(00012.HK) — article uses 恆, HKEX uses 恒"),
    ("潤地", "01109", ABBREV, "AASTOCKS 潤地(01109.HK)"),
    ("建板", "01888", ABBREV, "AASTOCKS 建板(01888.HK)"),
    ("民行", "01988", ABBREV, "AASTOCKS 民行(01988.HK)"),
    ("建行", "00939", ABBREV, "AASTOCKS 建行(00939.HK)"),
    ("中行", "03988", ABBREV, "AASTOCKS 中行(03988.HK)"),
    ("龍湖", "00960", ABBREV, "Ming Pao 龍湖半年少賺39%"),
    ("中石油", "00857", ABBREV, "Ming Pao 中石油多賺22%"),
    ("國航", "00753", ABBREV, "Ming Pao 國航次季盈轉虧"),
    ("中銀", "02388", ABBREV, "Ming Pao 中銀多賺7% (body: 中銀香港(2388))"),
    ("小米", "01810", ABBREV, "Ming Pao 小米折疊手機"),
    ("滙豐", "00005", ABBREV, "common HK spelling 滙, HKEX uses 匯"),
    ("京東", "09618", ABBREV, "common mention"),
    ("中石化", "00386", ABBREV, "common mention"),
    # Full names as written in the articles
    ("優地機器人", "03231", FULL, "AASTOCKS 優地機器人(03231.HK)"),
    ("中國財險", "02328", FULL, "AASTOCKS 中國財險(02328.HK)"),
    ("天數智芯", "09903", FULL, "AASTOCKS 天數智芯(09903.HK)"),
    ("順豐控股", "06936", FULL, "AASTOCKS 順豐控股(06936.HK)"),
    ("大新銀行", "02356", FULL, "Ming Pao 大新銀行多賺13%"),
    ("萬洲國際", "00288", FULL, "Ming Pao 萬洲國際(0288)"),
    ("麥科田", "02041", FULL, "Ming Pao 麥科田首日孖展"),
    ("江波龍", "09976", FULL, "Ming Pao 江波龍擬港上市"),
    ("納真科技", "09856", FULL, "Ming Pao 納真科技通過上市聆訊"),
    ("復星國際", "00656", FULL, "Yahoo HK 復星國際(00656)"),
    ("名創優品", "09896", FULL, "Yahoo HK 名創優品(09896.HK)"),
    ("智譜", "02513", FULL, "Yahoo HK 智譜(02513.HK)"),
    ("MINIMAX", "00100", FULL, "Yahoo HK MINIMAX(00100.HK)"),
    ("中國銀行", "03988", FULL, "full name of 中行"),
    ("香港交易所", "00388", FULL, "common mention"),
    # English names
    ("Tencent", "00700", ENGLISH, "English name"),
    ("Alibaba", "09988", ENGLISH, "English name — HKEX EN name is BABA-W"),
    ("HSBC", "00005", ENGLISH, "English name"),
    ("Meituan", "03690", ENGLISH, "English name"),
    ("BYD", "01211", ENGLISH, "English name"),
    ("Xiaomi", "01810", ENGLISH, "English name"),
    ("AIA", "01299", ENGLISH, "English name"),
    ("Ping An", "02318", ENGLISH, "English name"),
    ("CATL", "03750", ENGLISH, "English name"),
    ("SenseTime", "00020", ENGLISH, "English name"),
    ("PetroChina", "00857", ENGLISH, "English name"),
    ("Bank of China", "03988", ENGLISH, "English name"),
    ("Tencent Holdings", "00700", ENGLISH, "English full name"),
    ("HSBC Holdings plc", "00005", ENGLISH, "English full name"),
    # A/H dual-listed companies by their common (A-share-identical) names
    ("寧德時代", "03750", DUAL, "A+H"),
    ("中國平安", "02318", DUAL, "A+H"),
    ("招商銀行", "03968", DUAL, "A+H"),
    ("中國神華", "01088", DUAL, "A+H"),
    ("比亞迪股份", "01211", DUAL, "A+H"),
    # Companies not listed on HKEX — should miss
    ("花旗", None, MISS, "AASTOCKS 花旗"),
    ("美銀證券", None, MISS, "AASTOCKS 美銀證券"),
    ("中銀國際", None, MISS, "AASTOCKS 中銀國際 (unlisted BOC subsidiary)"),
    ("野村", None, MISS, "Yahoo HK 野村"),
    ("巴克萊", None, MISS, "AASTOCKS 巴克萊"),
    ("富達", None, MISS, "Ming Pao 富達"),
    ("海通國際", None, MISS, "Ming Pao 海通國際 (privatised)"),
    ("工銀亞洲", None, MISS, "Ming Pao 工銀亞洲 (privatised)"),
    ("58同城", None, MISS, "AASTOCKS 58同城"),
    ("長鑫", None, MISS, "Ming Pao 長鑫新內存晶片"),
    ("莫德納", None, MISS, "Ming Pao 莫德納"),
    ("克而瑞", None, MISS, "Ming Pao 克而瑞"),
    ("標普", None, MISS, "Ming Pao 標普"),
    ("Anthropic", None, MISS, "Ming Pao Anthropic"),
    ("OpenAI", None, MISS, "Yahoo HK OpenAI"),
    ("Nvidia", None, MISS, "Ming Pao Nvidia"),
    ("SB Energy", None, MISS, "Yahoo HK SB Energy"),
    ("雲夢海餐飲", None, MISS, "Ming Pao 雲夢海餐飲"),
    # Ambiguous shared-prefix names and generic terms
    ("平安", "02318", AMBIG, "vs 平安好醫生"),
    ("地產", None, AMBIG, "generic — AASTOCKS 地產股"),
    ("銀行", None, AMBIG, "generic"),
    ("內銀", None, AMBIG, "generic — AASTOCKS 內銀H股"),
    ("華潤", None, AMBIG, "group name, 11 listed CR companies"),
]
