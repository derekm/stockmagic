# stockmagic <-> stock_monitor — findings log

Running the two repos together (bridge + reconciliation). Findings dated.

## 2026-08-04 — Fisher reconciliation: real divergence explained

Ran `python -m src.adapter_stockmonitor --data-dir stock_monitor --universe SP500
--start-year 2015 --reconcile` after `git lfs pull` in stock_monitor.

Reconciliation output (last levels):
  fisher_p                 n=5480  sm_last=310330.26   smm_last=4207.59
  laspeyres_p              n=5480  sm_last=30055775.73 smm_last=4207.59
  paasche_p                n=5480  sm_last=111509.43    smm_last=4207.59
  chained_fisher_vs_sm     n=5480  sm_last=310330.26   smm_last=1113.84

ROOT CAUSE (not a bug in either repo — different index constructions):
  1. BASE SCALING: stock_monitor `fisher_index.py` starts chained levels at 100
     (doc line 19: "100 * cumulative product of links"; code line 118:
     `Fp = Fp = ... = 100.0`). stockmagic `IndexMath` defaults base_level=1000.
     => 10x factor on its own.
  2. QUANTITY SERIES: stock_monitor uses VOLUME as q (`panel()` pivot
     values="volume"); stockmagic uses SHARES*IWF (market_cap/close from
     fundamentals). Different q => different Paasche and therefore Fisher arms.

CONCLUSION: the two Fisher indexes are NOT the same index. stock_monitor's is a
volume-weighted chained Fisher (base 100); stockmagic's is a cap-weighted (shares)
chained Fisher (base 1000) with a rolling-base de-biasing path stock_monitor lacks.
The "missing link" is therefore: (a) document that they are different indexes, and
(b) make the reconciler normalize both to 100 at the overlap start so the
*residual* divergence (from the quantity choice + chaining method) is visible
separate from the trivial base-scaling.

## 2026-08-04 — LFS guard added

`src/data/market_data.py::_assert_real_parquet` now fails fast (clear "git lfs
pull" message) instead of DuckDB's cryptic "No magic bytes" when a parquet is an
LFS stub. `build_panel_from_parquet` calls it on the 3 input files. Verified: a
stub raises ValueError; a real parquet passes. RUNBOOK now documents `git lfs pull`.

## Data-coverage artifact (quality sweep)

`fundamentals.parquet` history starts ~1980; `daily_prices_clean` starts ~1962.
So the year-end quality sweep shows quality_pass=0 for 1962-1979 (no fundamentals
to gate on). Expected given PIT source coverage; not a bug. Worth noting in docs.
