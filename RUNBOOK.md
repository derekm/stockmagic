# Runbook

## Run the parallel-variant pipeline on real data
```bash
. .venv/Scripts/activate
PYTHONPATH=. python -m src.adapter_stockmonitor \
    --data-dir "C:/Users/derek/src/stockmagic/stock_monitor" \
    --universe SP500 --start-year 2015
```
Computes SIX index constructions at once (S&P value, Laspeyres, Paasche,
Fisher, chained Fisher, chained Laspeyres) and prints **live comparison metrics**:
`substitution_bias_ratio`, `delta_fisher_vs_chained`, and per-variant cumulative
return vs the S&P value benchmark.

## Unit tests (all variants)
```bash
PYTHONPATH=. python -m tests.test_index_math
```
Asserts: all variants compute; Fisher identity `F = sqrt(L*P)`; value =
price × quantity; event continuity across every variant's divisor; chained ≠
fixed-base (de-biasing active).

## The variant family (parallel divisor methodology)
| variant | construction | divisor family |
|---|---|---|
| `sp_value` | S&P cap-weighted value `ΣPQ/D` | S&P single divisor D |
| `laspeyres` | `ΣP_t Q_0 / ΣP_0 Q_0` | fixed-arm divisor |
| `paasche` | `ΣP_t Q_t / ΣP_0 Q_t` | fixed-arm divisor |
| `fisher` | `√(L_p·P_p)` | fixed-arm divisor |
| `chained_fisher` | chained links, rolling base | OUR parallel (rolling) divisor |
| `chained_laspeyres` | chained Laspeyres links | OUR parallel (rolling) divisor |

All live in the `index_levels` long table (keyed by `variant`); every divisor
is registered in `divisors`. `IndexMath.apply_event` re-scales all of them
atomically (S&P eq. 6 + arm ·k + chained post-event rescale).

## Known bridge limitations
- `share_counts` derived as `market_cap / close` from a single PIT snapshot, so
  `qty` is near-flat (no time-varying float). The fixed arms (L/P/F) therefore
  collapse together; the `chained_*` arms break away as designed.
- `ev_ebitda` is NULL for most PIT rows; `quality_value.qualify` treats NULL as
  "unknown" (does not disqualify) and reports `trifecta_coverage_avg`.
- PIT fundamentals are a single latest snapshot; historical re-computation of
  the quality gate needs a PIT snapshot timeseries + backfill (next work item).
