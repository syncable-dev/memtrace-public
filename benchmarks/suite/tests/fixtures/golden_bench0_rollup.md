# Bench #0 — Exact-Symbol Lookup

**Primary axis:** `acc_at_1_pct`
**Queries:** 5
**Dataset version:** test-fixture

| Adapter | Coverage | Acc@1 | Acc@5 | Acc@10 | MRR | Avg latency (ms) | Avg tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| memtrace | 80.0% | 60.0% | 80.0% | 80.0% | 0.700 | 9.20 | 157 |
| chromadb | 100.0% | 40.0% | 60.0% | 60.0% | 0.467 | 57.80 | 1930 |

## Primary axis result

✅ **memtrace wins** `acc_at_1_pct` (60.0% vs 40.0%).
