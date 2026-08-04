# stockmagic

StockMonitor suite — market-data capture + S&P DJI index mathematics with a
chained Fisher price/quantity decomposition and quality/value dual-pass gates.

> **Repo split (2026-08-03):** the original `stock_monitor/` package (portfolio
> tracking, forecasting, Granite TTM backfill, analytics dashboard) now lives
> in its **own repository** at `github.com/derekm/stock_monitor`. This repo
> (`stockmagic`) holds the core S&P index mathematics, Fisher decomposition, and
> quality/value gates. See the `stock_monitor` repo for the full toolkit.

## What's here

| File | Role |
|---|---|
| `src/data/market_data.py` | Raw feed capture (trades, prices, shares, sleeves) + audited clean panel (schema/jump/flatline); bridges real stock_monitor parquet via `build_panel_from_parquet`. |
| `src/data/pit_snapshots.py` | Point-in-time snapshot timeseries + backfill (daily forward-fill by `as_of <= date`); re-derives `market_cap` / `mktcap_to_assets` from the price panel; `qualify_as_of` builds the quality gate as of any historical date. |
| `src/analytics/index_math.py` | S&P divisor value index + fixed-base Laspeyres/Paasche/Fisher arms + OUR chained Fisher & chained Laspeyres. All six maintained in parallel in `index_levels`; `divisors` registry; `apply_event` re-scales every divisor atomically. |
| `src/analytics/quality_value.py` | Buffett ROE/ROIC, trifecta (EV/EBITDA<=9, P/B<=1.5, MktCap/Assets<=0.5), leverage flags, DuPont decomposition. NULL-safe, reports coverage. |
| `src/adapter_stockmonitor.py` | Runs the full pipeline over the real stock_monitor parquet store; emits live comparison metrics (`substitution_bias_ratio`, `delta_fisher_vs_chained`) and a year-end quality sweep across the whole variant family. |
| `sql/nominal_index_pipeline.sql` | Same math as a DuckDB-Wasm SQL script for SQL Lab. |
| `tests/test_index_math.py` | Synthetic-data property tests (all variants run; Fisher identity; value = price × quantity; event continuity; chained ≠ fixed-base). |
| `tests/test_pit_snapshots.py` | PIT snapshot forward-fill + quality gate as-of-date. |

## Design notes (from the S&P DJI methodology critique)

- The cap-weighted index is a **Laspeyres-derived, base-scaled** index. We keep
  S&P's divisor as the *continuity engine* for the aggregate market value.
- On top we hang the **full Fisher / Laspeyres / Paasche family**, each arm with
  its own divisor so continuity + Fisher symmetry hold — plus OUR **chained arms**
  (rolling base) that re-anchor continuously instead of accumulating substitution
  bias between discrete events.
- **Fisher quantity** is derived as `V / P_F`; it inherits continuity for free.
- `Nominal = P_F * Q_F` by construction → every period return splits cleanly
  into a valuation (price) component and a share/cap (quantity) component — the
  building block for the "Fisher quantity sleeve" in the macro layer.
- The divisor moves only on **non-market events** (add/delete, corp actions,
  share or IWF changes). `apply_event` re-scales the S&P divisor, all fixed arms,
  and the chained levels atomically.
- **Capping / AWF concentration limits are NOT implemented in this repo** (see
  `INDEX_MATH_METHODOLOGY.md` §3 — listed as a gap). The quality gate
  (Buffett / trifecta / leverage) is the dual-pass first leg, PIT-joined with no
  look-ahead.

## Run

```bash
# deps: duckdb, pytest (uv.lock present)
uv sync            # or: pip install duckdb pytest
python -m pytest tests/ -q
# or just the smoke tests:
python tests/test_index_math.py
python tests/test_pit_snapshots.py
```

The full six-variant pipeline over real data is driven by the adapter — see
`RUNBOOK.md`.
