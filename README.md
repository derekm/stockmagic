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
| `src/analytics/index_math.py` | S&P divisor value index + fixed-base Laspeyres/Paasche/Fisher arms + OUR chained Fisher, chained Laspeyres, and chained Paasche. All six maintained in parallel in `index_levels`; `divisors` registry; `apply_event` re-scales every divisor atomically. |
| `src/analytics/reconcile.py` | Reconciles stockmagic's `vol_chained_*` variants against `stock_monitor`'s `fisher_indexes_sp500.parquet` (same `sp500_member` sleeve) — validates the reimplementation; see `FINDINGS.md`. |
| `src/adapter_stockmonitor.py` | Runs the full pipeline over the real stock_monitor parquet store; emits live comparison metrics (`substitution_bias_ratio`, `delta_fisher_vs_chained`) and a year-end quality sweep across the whole variant family. |
| `sql/nominal_index_pipeline.sql` | Same math as a DuckDB-Wasm SQL script for SQL Lab. |
| `tests/test_index_math.py` | Synthetic-data property tests (all variants run; Fisher identity; value = price × quantity; event continuity; chained ≠ fixed-base). |
| `tests/test_pit_snapshots.py` | PIT snapshot forward-fill + quality gate as-of-date. |

## Docs

| Doc | What it covers |
|---|---|
| `INDEX_MATH_METHODOLOGY.md` | The index-number math: S&P divisor continuity + the full Fisher/Laspeyres/Paasche family in parallel; the chained (rolling-base) arms and why the 63-trading-day base is stockmagic's own parameter (not S&P methodology); reconciliation table + references. |
| `FINDINGS.md` | Dated findings log from running the two repos together — Fisher reconciliation root-cause + resolution, the LFS guard, and the **pass-5 out-of-sample Granite-TTM result** (pass-4 was in-sample memorization; trained on last 10y, Granite-TTM beats persistence on *direction* but not *level*). |
| `MISSING_LINKS.md` | The original "missing links" analysis — gaps where stockmagic and stock_monitor should connect (reconciliation, PIT bridge, quality gate). |
| `RUNBOOK.md` | How to run the parallel-variant pipeline on real data, verify, and the LFS pull step. |

## Index math at a glance

Six constructions on one `idx_panel` basket (`p_t` = price, `q_t` = quantity:
`q_t_sp = shares*IWF` for the cap-weighted family, `q_t_vol` = traded volume
for the `vol_*` family).

**S&P value (single divisor):** a float-adjusted market-cap aggregate normalized
so the base day equals `base_level`.

$$ V_t = \frac{\sum_i p_t \cdot q_t}{D}, \qquad D = \frac{\sum_{t_0} p_t \cdot q_t}{baselevel} $$

**Fixed-base arms** (each keeps its own divisor; `F = sqrt(L*P)` holds exactly):

$$ L_t = \frac{\sum p_t \cdot q_0}{\sum p_0 \cdot q_0}, \quad
   P_t = \frac{\sum p_t \cdot q_t}{\sum p_0 \cdot q_t}, \quad
   F_t = \sqrt{L_t \cdot P_t} $$

**Chained arms** (re-anchor every `chain_n = 63` trading days; link uses the
rolling base `b` and prior day `t-1`):

$$ L_p(t) = \frac{\sum p_t \cdot q_b}{\sum p_{t-1} \cdot q_b}, \quad
   P_p(t) = \frac{\sum p_t \cdot q_t}{\sum p_{t-1} \cdot q_t} $$

$$ level_t = baselevel \cdot \exp\!\left( \sum_{\tau \le t} \ln link_\tau \right) $$

**S&P vs stockmagic (important):** S&P 500 uses a *fixed* 1941-43 = 10 base
with a divisor that adjusts only on corporate actions — no periodic rebase. The
63-day rolling base is stockmagic's own chained-index choice (C-CPI-U style
substitution-bias reduction), **not** S&P methodology. Full detail and the
reconciliation table: `INDEX_MATH_METHODOLOGY.md`.

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
`RUNBOOK.md` for the run/verify commands and the `git lfs pull` step. For the
cross-repo reconciliation results and the Granite-TTM out-of-sample finding, see
`FINDINGS.md`.
