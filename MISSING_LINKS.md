# Missing links: stockmagic <-> stock_monitor

Analysis of the two repos run together. Each item below is a gap where the two
repos should connect but do not (or connect opaquely). Findings are grounded in
source reads + live runs (see [FINDINGS.md](FINDINGS.md)).

## The bridge (as built)

`src/adapter_stockmonitor.py` reads four parquet files from `../stock_monitor`:
`daily_prices_clean.parquet`, `fundamentals.parquet` (or `fundamentals_pit.parquet`),
`monitored_stocks.parquet`. It builds `idx_panel` (`P_t = close`,
`Q_t = shares*IWF`), runs the 19-variant index math (7 constructions across `sp_*`/`vol_*`/`trad_*` families), the PIT backfill + quality
gate, and a family comparison. `src/analytics/reconcile.py` (optional, `--reconcile`)
compares stockmagic's `index_levels` against stock_monitor's `fisher_indexes.parquet`.

## Missing links

### 1. LFS data precondition (BLOCKER — now guarded)
The four parquet files are Git-LFS pointers in a fresh stockmagic checkout.
Running the adapter before `git lfs pull` threw DuckDB's cryptic
`No magic bytes found`. FIXED: `market_data._assert_real_parquet` fails fast with a
clear "run git lfs pull" message. RUNBOOK now documents the prerequisite.

### 2. Fisher reconciliation (DONE, revealed a structural divergence)
stock_monitor computes its own Fisher index (`fisher_index.py` -> `fisher_indexes.parquet`)
over the same data stockmagic bridges in. stockmagic reimplements the math
independently. The reconciler now joins them on date. FINDING (see [FINDINGS.md](FINDINGS.md)):
the two are NOT the same index —
  - stock_monitor chained levels start at **base 100**; stockmagic at **base 1000**.
  - stock_monitor uses **volume** as quantity `q`; stockmagic uses **shares*IWF**.
After re-basing both to 100 at the overlap start, the residual still grows to
~20x on `fisher_p` — i.e. a structural, time-varying divergence, not a scaling bug.
The reconciler's job is now to *prove and quantify* that they are different
constructions (volume-weighted chained vs cap-weighted divisor-based chained),
which is the honest state. No further "fix" is warranted; document it.

### 3. Equal-weight arms (partial — out of scope by design)
stock_monitor builds equal-weight indexes (`build_defensive_index.py`,
`build_growth_tech_index.py` -> `defensive_value_index.parquet`, `growth_tech_index.parquet`).
stockmagic lists equal-weight as "out of scope / lives in stock_monitor" but never
reads those outputs. `defensive_value_index.parquet` is a 1-row summary (no time
series); `growth_tech_index.parquet` is a membership/weight table. A cap-weighted
vs equal-weight tilt metric is feasible only against a time series, which
stock_monitor does not emit for these sleeves. LINK: emit a time-series equal-weight
level from stock_monitor (or compute it inside stockmagic from the same panel) so
the "out of scope" claim is measured, not asserted.

### 4. Quality-gate cross-validation (not done)
Both repos have a quality gate: stock_monitor's canonical `quality_gate_bridge.py`
and stockmagic's parallel `quality_value.py`. They are never run on the same names
and compared. LINK: run both on the same `pit_snapshots` and report
agreement/disagreement (the two should flag the same set; if not, investigate).

### 5. Sleeve-membership consistency (not done)
stockmagic derives `sleeve_tag` from `monitored_stocks` (`sp500_member`,
`growth_tech_index`, `defensive_value_index`); stock_monitor's `build_index.py` /
`build_*_index.py` are the membership authority. They can silently disagree.
LINK: assert stockmagic's derived membership matches stock_monitor's index builds
and alert on drift.

### 6. Nested copy instead of a reference (structural)
stockmagic carries a *copy* of stock_monitor under `stockmagic/stock_monitor/`.
It is a snapshot that drifts from stock_monitor's true state and (until `git lfs
pull`) is stubbed. LINK: replace with a git submodule or a shared data volume so
stockmagic tracks stock_monitor's real, LFS-pulled store.

### 7. Column-contract test (now partially covered)
`build_panel_from_parquet` assumes specific columns (`sp500_member`,
`growth_tech_index`, `as_of_date`, `market_cap_b`, ...). A renamed/missing column
breaks the pipeline opaquely mid-SQL. PARTIAL FIX: the LFS guard catches the worst
case (missing file), but a schema-assertion check at bridge entry would fail fast
on column drift. RECOMMENDED: add a schema-assertion step that lists expected
columns and raises with the missing ones named.

## Implementation status
- #1 LFS guard: implemented (`_assert_real_parquet`).
- #2 reconcile: implemented (`reconcile.py` + `--reconcile`); revealed structural
  divergence, documented.
- #3-#7: identified; #3/#4/#5 need a data emit or a second run; #6/#7 are
  structural hygiene. [FINDINGS.md](FINDINGS.md) tracks the live-run evidence.

## See also

- [README.md](README.md) — repo overview, module map, doc index, and index-math formulas.
- [INDEX_MATH_METHODOLOGY.md](INDEX_MATH_METHODOLOGY.md) — full methodology + reconciliation table + references.
- [FINDINGS.md](FINDINGS.md) — dated cross-repo findings (reconciliation resolution, pass-5 OOS Granite result).
- [RUNBOOK.md](RUNBOOK.md) — how to run the pipeline on real data.
