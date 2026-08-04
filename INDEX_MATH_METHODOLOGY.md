# StockMonitor Index Mathematics — Methodology & Innovations

This document is written in the spirit of the S&P Dow Jones Indices
*Index Mathematics Methodology* (March 2025). It states, precisely, what we
borrow from S&P, what we add on top of S&P, and why our additions are
superior and/or complementary for *active, multi-sleeve fund management* whose
goal is to beat a passive S&P path over a decade while avoiding permanent
capital loss.

---

## 1. What we borrow from S&P DJI (and why it is correct)

### 1.1 The divisor as a continuity engine
S&P defines a cap-weighted index as

$$ Index_t = \frac{\sum_i P_{i,t} \, Q_i}{D_t} $$

and explicitly notes this is a *modification of a Laspeyres index*: the
Laspeyres base-period quantities $Q_0$ are replaced by current quantities $Q_1$, and
the denominator $\sum_i P_0 Q_0$ is replaced by a **divisor** $D$ that both encodes the
initial market value and fixes the base level (e.g., 1000).

We adopt this verbatim. The divisor is the single mechanism that lets the index
absorb **non-market events** — constituent additions/deletions, corporate
actions, share-count or IWF changes — without the level jumping. This is exactly
what a fund needs: rebalances and index maintenance must be invisible to the
benchmark path, otherwise "outperformance" would be an artifact of our own
mechanics.

### 1.2 Multiplicative divisor adjustment (S&P eq. 6)
On an event that changes market value by factor $k$ at constant prices,

$$ D_{new} = D_{old} \cdot k $$

keeps the level flat across the event. Our `IndexMath.apply_event` implements
exactly this. S&P also offers an additive form (eq. 7),

$$ D_{new} = D_{old} + \frac{CMV}{IndexLevel} $$

where $CMV$ is the change in market value; we use the multiplicative
form because it composes cleanly with chained Fisher arms (see §2.2).

### 1.3 Total-return extension by dividend points
S&P builds a total-return index by converting daily dividends to *index points*
via $IndexDividend = TotalDailyDividend / D$. We borrow the same idea: any
income stream (dividends, repo, carry) is expressed in divisor units so it
scales with the same continuity logic. (Wired but not yet exercised in the
current pipeline; the hook `divisor` is the shared denominator.)

---

## 2. What we add over S&P (our innovations)

### 2.1 The full Fisher/Laspeyres/Paasche family, maintained in PARALLEL
S&P's index is a *single* Laspeyres-derived number. It cannot be decomposed
into a pure price move and a pure quantity (capital-structure) move, because it
has no Paasche arm. We compute the **entire index-number family at once** on the
same basket, so they can be compared, blended, and stress-tested without
rebuilding the pipeline:

$$ L_p = \frac{\sum_i P_t Q_0}{\sum_i P_0 Q_0},\quad P_p = \frac{\sum_i P_t Q_t}{\sum_i P_0 Q_t} $$

(Laspeyres price, base shares; Paasche price, current shares)

$$ F = \sqrt{L_p \cdot P_p},\quad V_t = \frac{\sum_i P_t Q_t}{D_t},\quad Q_F = \frac{V_t}{F} $$

(Fisher price = ideal geometric mean; $V_t$ = S&P value index by construction; $Q_F$ = derived Fisher quantity, continuity inherited)

Every variant is stored in one long `index_levels` table keyed by `variant`, and
a `divisors` registry tracks each variant's divisor. This is the structural
improvement: **all constructions are first-class and co-maintained**, so the fund
can run S&P, Laspeyres, Paasche, and Fisher side by side and read the spread
between them as a live signal (e.g. L−P spread = the substitution/drift bias).

**Why this is superior for fund management:** every period return splits
geometrically into a *valuation* component (`ret_price`) and a *capital-structure*
component (`ret_qty`):

$$ ret_{total} = ret_{price} \cdot ret_{qty} $$

`ret_qty` isolates share issuance / buyback / float drift — information a
cap-weighted S&P path hides. Fisher also satisfies the time-reversal and
factor-reversal tests that Laspeyres and Paasche each fail individually.

### 2.2 Our parallel divisor methodology: chained (rolling-base) re-anchoring
S&P keeps a *fixed* base ($Q_0$ from the inception date) and only re-scales via the
divisor at discrete events. Between events, weights drift with price — the well
known **large-cap momentum bias** of cap-weighting (overweighting past winners).

We add a **chained** divisor methodology that re-anchors the base window every
`chain_n` trading days and chains period-over-period *links*:

$$ link_t = Fisher( basket_t,\ basket_{t-1},\ base\ window ) $$

$$ level_t = baselevel \cdot \prod_{\tau \le t} link_\tau $$

This is the same chaining used in `stock_monitor/fisher_index.py` (and standard
in official Fisher series). Unlike S&P's single divisor, our chained arms carry
**no fixed** $Q_0$ to drift — the base rolls continuously. We maintain **both**
families at once:

  * S&P single divisor $D$ + fixed-base arm divisors ($L, P, F$) — for exact
    continuity and the Fisher identity $F = \sqrt{L \cdot P}$.
  * OUR chained divisors (`chained_fisher`, `chained_laspeyres`) — for the
    de-biased path.

The fund can *measure* the bias it is avoiding (live `substitution_bias_ratio`,
`delta_fisher_vs_chained`) rather than assert it.

### 2.3 Divisor events maintained across ALL variants atomically
A non-market event (add/delete, corp action) is absorbed by re-scaling every
maintained divisor at once in `apply_event`:
  * S&P divisor $D \to D \cdot k$ (S&P eq. 6), value index flat.
  * fixed arms divisors ($L, P, F$) $\to \cdot k$, symmetry preserved.
  * chained arms → post-event levels rescaled by k (the rolling base already
    makes the *path* event-invariant, so this is continuity only).

This is the complementary bridge: **S&P continuity for composition changes +
Fisher idealness for price/quantity separation + our chained path for de-biasing.**

### 2.4 Point-in-time fundamentals for the quality gate
S&P math is purely price/quantity. We bolt on a PIT-aware quality/value gate
(`quality_value.py`) — Buffett ROE/ROIC, the trifecta (EV/EBITDA ≤ 9, P/B ≤ 1.5,
MktCap/Assets ≤ 0.5), leverage flags, DuPont — joined with `as_of ≤ date` so the
dual-pass screen has **no look-ahead bias**. NULL metrics are treated as
"unknown" (do not disqualify) and `trifecta_coverage` is reported so screening
quality is auditable.

---

## 3. Superior / complementary summary

Honest two-way comparison against the S&P DJI *Index Mathematics* methodology
(April 2026). "S&P" = what the S&P methodology defines; "stockmagic" = what
`src/analytics/index_math.py` actually implements today.

| Concern | S&P DJI methodology | stockmagic today | Status / note |
|---|---|---|---|
| Continuity across rebalances (single divisor) | Yes — `D` rescaled on non-market events (eq. 6 / eq. 7) | Yes — `value_index` + `apply_event` use eq. 6 verbatim | Adopted as-is |
| Full Laspeyres / Paasche / Fisher family in parallel | No — single Laspeyres-derived number | Yes — 6 variants co-maintained in `index_levels` | Our addition |
| Price vs quantity separation (`ret_qty` isolation) | No — single number hides capital-structure drift | Yes — `decompose()` splits `ret_total = ret_price · ret_qty` | Our addition |
| Substitution / momentum bias | Acknowledged; mitigated by capping, not chaining | Yes — chained (rolling-base) arms re-anchor every `chain_n` days | Our addition; measurable vs fixed |
| Ideal index properties (time/factor reversal) | Laspeyres & Paasche each fail | Yes — Fisher `F = sqrt(L·P)` satisfies both | Our addition |
| Atomic event across the whole variant set | N/A per single index | Yes — one `apply_event` re-scales S&P + fixed arms + chained | Our addition |
| Float adjustment (IWF) | Yes — `Q_i = IWF_i · shares_i` (eq. 3) | Yes — `Q_t = shares · IWF` in `build_clean_panel` | Adopted as-is |
| Capped / concentration limits (AWF) | Yes — single-company + group capping, iterative redistribution | No — all 6 variants unconstrained | **Gap** |
| Equal-weight index (AWF = `Z / (N · floatadj MV)`) | Yes — periodic rebalance | No — not built in `index_math` (equal-weight lives in `stock_monitor/build_*.py`) | **Gap** (out of this module) |
| Price-weighted index | Yes — divisor adjusts on corp actions | No | **Gap** |
| Total-return (dividend points) | Yes — `IndexDividend = TotalDailyDividend / D` | Wired (divisor hook) but **not exercised** | **Partial** |
| Multi-day (smoothed) rebalancing | Yes — glide-path weights | No | **Gap** |
| Point-in-time quality/value gate | Out of scope (pure price/qty) | Yes — `quality_value.py`, PIT, no look-ahead | Our addition |
| All constructions at once (compare/blend/stress) | Rebuild per index | Yes — one `run_all`, long `index_levels` table | Our addition |

**Net:** S&P gives us a correct, industry-standard *continuity primitive* (divisor
**+** float adjustment **+** capping). We wrap it with a Fisher decomposition and chaining
that turn a single cap-weighted number into a multi-variant, de-biased signal set —
while the capping, equal-weight, price-weight, multi-day-rebalance, and exercised
total-return paths remain S&P features we have **not** reimplemented in this module.

### 3.1 Extended S&P sections — coverage status

The S&P methodology covers more than the core divisor/Fisher math. Coverage of the
remaining sections by `src/analytics/index_math.py` (and the broader repo):

| S&P section | What it defines | stockmagic status |
|---|---|---|
| DCR vs Divisor equivalence | Proves the divisor-based level and DCR (chain period returns) give identical same-currency series | **Both exercised** — `value_index` is the divisor form; the chained arms are exactly the DCR form (period links multiplied). They must agree on a single-currency basket — a built-in cross-check |
| Capped (weight) indices — AWF | `AWF_i = CW_i / W_i`, iterative single-company + group capping | Gap (see §3 table) |
| Capped **return** indices | Return since last rebalance capped at a preset level | Gap (distinct from weight capping) |
| Equal-weight / price-weight | AWF / price-only weighting | Gap (equal-weight lives in `stock_monitor/build_*.py`) |
| Total-return (dividend points) | `IndexDividend = TotalDailyDividend / D` | Wired (divisor hook), not exercised |
| **Dividend Points Indices** | Cumulative running total of constituent dividends (reset quarterly/annual) | Gap — no dividend-points series emitted |
| **Index Turnover** | One-way turnover from events/rebalances, `sum | w_CLS - w_ADJ |` | Gap — not computed |
| Alternative pricing (SOQ / VWAP / TWAP / Fair Value) | Open / VWAP / TWAP prices instead of close | Gap — `P_t = adjclose` only; data-source choice, not a math gap |
| Currency-hedged / Quanto / PL-adjusted | FX forward hedging, quanto, price-level translation | Out of scope (single-currency US basket) |
| Risk Control (vol targeting), 2.0, min-var | Dynamic leverage from realized vol | Out of scope |
| Weighted-Return (index-of-indices) | Combine component index returns | Out of scope |
| Leveraged / Inverse | `K×` return `±` borrowing/lending | Out of scope |
| Fee / Decrement / Increment | Synthetic fee/dividend as index-point drag | Out of scope |
| Negative/Zero levels | Floor at zero for leveraged/inverse | N/A (no leverage) |
| EOM Global Fundamental Data | P/E, P/B, ROE, yields via AWF/IWF | Partially covered by `quality_value.py` (PIT ROE / DuPont / trifecta), not the S&P EOM ratio set |

**Net:** stockmagic's scope is the **index-number core** — divisor continuity + Fisher
decomposition + chaining + PIT quality gate. The sections above fall into three
buckets: out-of-scope by design (the derivative / leverage / fee families),
single-currency simplifications (FX), and concrete gaps to fill later
(capped-return, dividend-points, turnover). The DCR/divisor equivalence is the one
place stockmagic **already** implements both S&P forms.

---

## 4. Pipeline layout (this repo)

| File | Role |
|---|---|
| `src/data/market_data.py` | Capture (trades, prices, shares, sleeves) + PIT fundamentals; audited clean panel; `build_panel_from_parquet` bridges real `stock_monitor` parquet |
| `src/analytics/index_math.py` | `IndexMath`: S&P value index (single divisor) + fixed-base Laspeyres/Paasche/Fisher arms + OUR chained Fisher & chained Laspeyres — **all six maintained in parallel** in `index_levels`; `divisors` registry; `apply_event` re-scales every divisor atomically |
| `src/analytics/quality_value.py` | Buffett/trifecta/leverage/DuPont gates; NULL-safe; reports coverage |
| `src/adapter_stockmonitor.py` | Runs the full pipeline over the real `stock_monitor` parquet store; emits live comparison metrics across the whole variant family |
| `sql/nominal_index_pipeline.sql` | Same math as DuckDB-Wasm SQL for the dashboard SQL Lab |
| `tests/test_index_math.py` | Synthetic property tests: all variants run, Fisher identity `F=sqrt(L·P)`, value = price×quantity, event continuity across all variants, chained ≠ fixed |

See `RUNBOOK.md` for the run/verify commands.
