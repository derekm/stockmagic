# Runbook

## Run the parallel-variant pipeline on real data
```bash
# PREREQUISITE: the stock_monitor parquet files are Git-LFS pointers in a fresh
# checkout. Pull them or the bridge fails with a clear "git lfs pull" message:
cd stock_monitor && git lfs pull && cd ..

. .venv/Scripts/activate
PYTHONPATH=. python -m src.adapter_stockmonitor \
    --data-dir "C:/Users/derek/src/stockmagic/stock_monitor" \
    --universe SP500 --start-year 2015
```
Computes SEVEN index constructions at once (S&P value, Laspeyres, Paasche,
Fisher, chained Fisher, chained Laspeyres) and prints **live comparison metrics**:
`substitution_bias_ratio`, `delta_fisher_vs_chained`, and per-variant cumulative
return vs the S&P value benchmark. It also runs the PIT layer (below) and prints a
year-end quality sweep.

## Reconcile against stock_monitor's own Fisher index (validates the reimplementation)
```bash
PYTHONPATH=. python -m src.adapter_stockmonitor \
    --data-dir "C:/Users/derek/src/stockmagic/stock_monitor" \
    --universe SP500 --start-year 2015 --reconcile
```
Joins stockmagic `index_levels` to stock_monitor `fisher_indexes.parquet` on date
and reports raw + re-based (`norm`) divergence per arm. The two are DIFFERENT
indexes — stock_monitor uses base 100 + volume as quantity; stockmagic uses base
1000 + shares*IWF. The reconciler's job is to *quantify* that divergence, not
eliminate it. See [FINDINGS.md](FINDINGS.md) / [MISSING_LINKS.md](MISSING_LINKS.md) for the analysis.

## Pipeline layers (in execution order)
1. **Panel bridge** — `MarketDataStore.build_panel_from_parquet` registers the
   real `stock_monitor` parquet as DuckDB tables (`daily_prices`, `share_counts`,
   `sp500_tags`, `pit_fundamentals`) and builds `idx_panel`
   (`P_t = adj_close`, `Q_t = shares * IWF`, `MV_t = P_t * Q_t`).
2. **PIT snapshot backfill** — `pit_snapshots.build_snapshot_timeseries` produces
   a daily `pit_snapshots` table: for each trading date, each ticker carries the
   most-recently-available fundamentals (`as_of <= date`), with `market_cap` /
   `mktcap_to_assets` re-derived from the price panel so capital-structure metrics
   move with the market. `pit_snapshots.qualify_as_of(store, as_of)` then builds
   the `quality_pass` gate as of any historical date (no look-ahead).
3. **Index math** — `IndexMath.run_all` runs the value index, fixed-base arms,
   chained arms, unifies them into `index_levels`, and decomposes each into
   `ret_total = ret_price * ret_qty`.
4. **Comparison + quality sweep** — the adapter pivots `index_levels`, prints the
   family comparison and the year-end `quality_pass` counts.

## Unit tests
```bash
PYTHONPATH=. python -m tests.test_index_math
PYTHONPATH=. python -m tests.test_pit_snapshots
```
`test_index_math` asserts: all variants compute; Fisher identity `F = sqrt(L*P)`;
value = price x quantity; event continuity across every variant's divisor; chained
!= fixed-base (de-biasing active). `test_pit_snapshots` asserts the daily
forward-fill PIT contract (no future data leaks) and the as-of quality gate.

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
- The PIT backfill (`pit_snapshots.build_snapshot_timeseries`) is implemented as a
  daily forward-fill keyed on `as_of <= date`. With only a single latest source
  snapshot the backfill is a constant step function (PIT-correct, but not
  historical). Genuinely time-varying backfill requires a multi-dated fundamentals
  source (e.g. `fundamentals.parquet` quarterly history) — the routine stitches
  those automatically when present.
- Capping / AWF concentration limits are not implemented (see
  [INDEX_MATH_METHODOLOGY.md](INDEX_MATH_METHODOLOGY.md) §3).

## See also

- [README.md](README.md) — repo overview, module map, doc index, and index-math formulas.
- [INDEX_MATH_METHODOLOGY.md](INDEX_MATH_METHODOLOGY.md) — full methodology + reconciliation table + references.
- [FINDINGS.md](FINDINGS.md) — dated cross-repo findings (reconciliation resolution, pass-5 OOS Granite result).
- [MISSING_LINKS.md](MISSING_LINKS.md) — the original missing-links analysis.
