"""I-1 pre-traffic part: per-call latency of the fuzzy path over the full live list, and how long a call
blocks the asyncio event loop (heartbeat-gap test).

    stock-assistant-pipeline-intelligence/.venv/bin/python poc/sapi/fuzzy-match/bench.py
"""

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES  # noqa: E402
from match_cases import ENTRIES, lookup_stock  # noqa: E402
from match_revised import STEPS, lookup, lookup_indexed  # noqa: E402

REVISED = STEPS[-1][1]
QUERIES = [q for q, *_ in CASES]
ROUNDS = 20
HEARTBEAT_S = 0.001


def pct(samples: list[float], p: float) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[p - 1]


def latency(name: str, fn) -> None:
    samples = []
    for _ in range(ROUNDS):
        for q in QUERIES:
            start = time.perf_counter()
            fn(q)
            samples.append((time.perf_counter() - start) * 1000)
    worst_q = max(QUERIES, key=lambda q: _time_once(fn, q))
    print(f"| {name} | {len(samples)} | {pct(samples, 50):.2f} | {pct(samples, 95):.2f} | {pct(samples, 99):.2f} "
          f"| {max(samples):.2f} | {worst_q} |")


def _time_once(fn, q: str) -> float:
    start = time.perf_counter()
    fn(q)
    return time.perf_counter() - start


async def heartbeat_gap(name: str, fn) -> None:
    """A 1 ms ticker runs beside a consumer that calls fn back to back; the largest tick gap is how long
    one call holds the loop. The baseline row runs no lookups, to show the ticker's own jitter."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(HEARTBEAT_S)
            now = time.perf_counter()
            gaps.append((now - last) * 1000)
            last = now

    async def consumer() -> None:
        for _ in range(5):
            for q in QUERIES:
                if fn is not None:
                    fn(q)
                await asyncio.sleep(0)  # yield between calls, as an FC loop would between awaits
        stop.set()

    if fn is None:
        task = asyncio.create_task(ticker())
        await asyncio.sleep(2)
        stop.set()
        await task
    else:
        await asyncio.gather(ticker(), consumer())
    print(f"| {name} | {len(gaps)} | {pct(gaps, 50):.2f} | {pct(gaps, 99):.2f} | {max(gaps):.2f} |")


def main() -> None:
    print(f"Python {sys.version.split()[0]}, {len(ENTRIES)} entries, {len(QUERIES)} queries x {ROUNDS} rounds\n")
    print("| Algorithm | Calls | p50 ms | p95 ms | p99 ms | max ms | Slowest query |")
    print("|---|---|---|---|---|---|---|")
    latency("§4.2 as written", lambda q: lookup_stock(q, 0.6))
    latency("revised (R1–R7)", lambda q: lookup(q, **REVISED))
    latency("revised, names normalized at load", lookup_indexed)

    print("\n| Heartbeat run | Ticks | p50 gap ms | p99 gap ms | max gap ms |")
    print("|---|---|---|---|---|")
    asyncio.run(heartbeat_gap("idle (no lookups)", None))
    asyncio.run(heartbeat_gap("§4.2 as written", lambda q: lookup_stock(q, 0.6)))
    asyncio.run(heartbeat_gap("revised (R1–R7)", lambda q: lookup(q, **REVISED)))
    asyncio.run(heartbeat_gap("revised, names normalized at load", lookup_indexed))


if __name__ == "__main__":
    main()
