"""Print every `name(code)` run in the crawled news articles, as raw material for data/news_cases.py.

Export the articles first (local SADI DB):
    docker compose exec -T postgres psql -U postgres -d sadi -At -F $'\\t' -c "select r.source_name, r.raw_id,
      r.title, coalesce(c.body_cleaned, r.body), r.extra_metadata from raw_news r left join cleaned_news c
      using (raw_id) where r.source_name <> 'HKEX'" > data/news_articles.tsv
(tabs/newlines inside fields replaced by spaces). The runs keep the words before the name, so each is
trimmed by hand.
"""

import collections
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
HK_RUN = re.compile(r"([A-Za-z一-鿿Ａ-Ｚ\-－&]{1,20})[\(（](\d{1,5})(?:\.HK)?[\)）]")
NON_HK_RUN = re.compile(r"([A-Za-z一-鿿]{1,20})[\(（](\d{6}\.(?:SH|SZ)|[A-Z]{1,5}\.US)[\)）]")

hk, non_hk = collections.Counter(), collections.Counter()
for line in (HERE / "data/news_articles.tsv").read_text().splitlines():
    _, _, title, body, _ = line.split("\t")
    text = f"{title} {body}"
    hk.update((m.group(1), m.group(2).zfill(5)) for m in HK_RUN.finditer(text))
    non_hk.update((m.group(1), m.group(2)) for m in NON_HK_RUN.finditer(text))
for (name, code), n in sorted(hk.items()):
    print("HK", name, code, n)
for (name, code), n in sorted(non_hk.items()):
    print("NON_HK", name, code, n)
