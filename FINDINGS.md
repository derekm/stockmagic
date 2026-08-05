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

> RESOLVED later the same day: the base 100/1000 mismatch is now cosmetic (the
> reconciler re-bases both to 100 at the overlap start, so `norm` cancels it), and
> the real residual is the **rolling-base window** (stockmagic's 63-day chained base
> vs stock_monitor's t-1->t link) — that is the *intended* ~<1% gap, not a bug. The
> earlier n=5480 numbers came from comparing stock_monitor's *portfolio* (8-name)
> basket against stockmagic's SP500 index; the reconciler now defaults to
> `fisher_indexes_sp500.parquet` (same `sp500_member` sleeve) and the missing
> chained-Paasche arm was added (`vol_chained_paasche`). Full write-up:
> `INDEX_MATH_METHODOLOGY.md` §5 (reconciliation table) and §6 (references), plus
> the S&P-500-vs-rolling-base contrast in §2.2.

## 2026-08-04 — LFS guard added

`src/data/market_data.py::_assert_real_parquet` now fails fast (clear "git lfs
pull" message) instead of DuckDB's cryptic "No magic bytes" when a parquet is an
LFS stub. `build_panel_from_parquet` calls it on the 3 input files. Verified: a
stub raises ValueError; a real parquet passes. RUNBOOK now documents `git lfs pull`.

## Data-coverage artifact (quality sweep)

`fundamentals.parquet` history starts ~1980; `daily_prices_clean` starts ~1962.
So the year-end quality sweep shows quality_pass=0 for 1962-1979 (no fundamentals
to gate on). Expected given PIT source coverage; not a bug. Worth noting in docs.

## 2026-08-04 — pass-5: honest out-of-sample Granite-TTM eval (pass-4 was in-sample)

`pass4.py` (the earlier sweep) reported near-zero MAPE (e.g. `half_wstride256`
AEP = 0.26%). That was **memorization, not forecasting**:

- `train_score_p3`/`train_score_p2` train AND score on the **same** windows
  (in-sample). `half_wstride256` had only **nw=8** windows on the 10y clip — the
  model memorized ~8 windows.
- It warm-started from a checkpoint trained on **all** history (holdout
  contamination).

Neural TTM models cannot be "backtested" on data they trained on, so `pass5.py`
does a temporally-disjoint holdout. Two protocols:

- `trainlast` (default): train on the **last 10y** (what production uses,
  `RECENT=2520`), test by forecasting the **preceding 10y** — disjoint, tests the
  real production regime.
- `half`: train first half of history, test second half.

Trained from the IBM base only (`pretrained=False`); persistence baseline computed
on the same test windows.

**Measured result** (AEP/NVR/FICO, fixed200/scaled400, 6000 steps, test =
preceding-10y block):

| ticker | config | n_test | model MAPE | persistence | model dir | pers dir |
|---|---|---|---|---|---|---|
| AEP | fixed200 | 200 | 8.33% | 5.87% | 67.5% | 33.0% |
| NVR | fixed200 | 200 | 8.74% | 7.96% | 60.5% | 33.0% |
| FICO | fixed200 | 200 | 16.61% | 10.40% | 57.5% | 36.0% |

Aggregate over all 12 configs: **0/12 beat persistence on MAPE**, but **12/12 beat
persistence on direction** (mean model dir_acc **59.8%** vs persistence **34.0%**,
≈ +26 pts). The stride configs (n=8–15 test windows) no longer show the pass-4
"magic" — they sit at 6–18% OOS too, confirming the leakage.

**Conclusion:** trained on the last 10y, Granite-TTM is a **direction forecaster,
not a level forecaster** — it beats naive persistence on *which way* the 96-day
move goes (~60% vs ~34%) but not on *how far* (its point forecast is noisier than
holding the last price, MAPE worse than persistence). Usable as a timing/tilt
signal; not competitive with persistence for precise price-level forecasts.

The harness lives in `stock_monitor/pass5.py` (not yet committed — should be, so
the honest eval isn't lost and pass-4's leakage isn't repeated). Caveats: 6000
steps may still be undertrained for some configs; one 10y test block; a rolling
multi-origin test would firm up the direction edge.

## See also

- `README.md` — repo overview, module map, doc index, and index-math formulas.
- `INDEX_MATH_METHODOLOGY.md` — full methodology + reconciliation table + references.
- `MISSING_LINKS.md` — the original missing-links analysis.
- `RUNBOOK.md` — how to run the pipeline on real data.
