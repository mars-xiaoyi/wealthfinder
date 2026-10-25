"""Fetch the live HK Stock List with SAPI's own loader and save it as a JSON snapshot.

Run from the repo root with SAPI's venv:
    stock-assistant-pipeline-intelligence/.venv/bin/python poc/sapi/fuzzy-match/fetch_stock_list.py
"""

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "stock-assistant-pipeline-intelligence"))

import httpx  # noqa: E402

from app.common.hk_stock_list import _fetch_and_filter_hk_stock_list  # noqa: E402


async def main() -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        entries = await _fetch_and_filter_hk_stock_list(client)
    out = HERE / "stock_list_snapshot.json"
    out.write_text(json.dumps({k: asdict(v) for k, v in entries.items()}, ensure_ascii=False, indent=1))
    print(f"{len(entries)} equities -> {out}")


asyncio.run(main())
