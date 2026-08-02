# Innovations Runbook

## Run the pipeline on real data (bridge → stock_monitor parquet)
```bash
. .venv/Scripts/activate
PYTHONPATH=. python -m src.adapter_stockmonitor \
    --data-dir "C:/Users/derek/src/stockmagic/stock_monitor" \
    --universe SP500 --start-year 2015
```
Prints both index modes (chained = our innovation, fixed = S&P-like baseline)
and **live comparison metrics**: `substitution_bias_ratio`,
`fisher_price_divergence`, `qty_chain_last` vs `qty_fixed_last`.

## Unit tests (both modes)
```bash
PYTHONPATH=. python -m tests.test_index_math
```
Asserts: Fisher identity, chaining continuity across a divisor event, and that
chained ≠ fixed (de-biasing active).

## Modes in IndexMath
- `mode="chained"` — rolling base re-anchor every `chain_n` days (our innovation)
- `mode="fixed"`   — S&P-like fixed base, divisor-only (baseline for comparison)

## Known bridge limitations
- `share_counts` derived as `market_cap / close` from a single PIT snapshot, so
  `qty` is near-flat (no time-varying float). The metrics still expose the
  *structural* bias of fixed vs chained bases.
- `ev_ebitda` is NULL for most PIT rows; `quality_value.qualify` treats NULL as
  "unknown" (does not disqualify) and reports `trifecta_coverage_avg`.
