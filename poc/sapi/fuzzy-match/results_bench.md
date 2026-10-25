Python 3.12.14, 2798 entries, 79 queries x 20 rounds

| Algorithm | Calls | p50 ms | p95 ms | p99 ms | max ms | Slowest query |
|---|---|---|---|---|---|---|
| §4.2 as written | 1580 | 2.33 | 5.63 | 11.02 | 11.36 | PetroChina |
| revised (R1–R7) | 1580 | 8.84 | 10.14 | 10.34 | 11.65 | 香港交易所 |
| revised, names normalized at load | 1580 | 2.06 | 2.94 | 3.05 | 3.70 | 香港交易所 |

| Heartbeat run | Ticks | p50 gap ms | p99 gap ms | max gap ms |
|---|---|---|---|---|
| idle (no lookups) | 1548 | 1.28 | 1.34 | 6.97 |
| §4.2 as written | 132 | 7.29 | 19.76 | 20.15 |
| revised (R1–R7) | 132 | 26.82 | 29.99 | 30.13 |
| revised, names normalized at load | 111 | 6.68 | 8.38 | 8.80 |
