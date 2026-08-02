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

    Index_t = Σ (P_i,t · Q_i) / D_t                                  (1)

and explicitly notes this is a *modification of a Laspeyres index*: the
Laspeyres base-period quantities Q_0 are replaced by current quantities Q_1, and
the denominator Σ P_0 Q_0 is replaced by a **divisor** D that both encodes the
initial market value and fixes the base level (e.g., 1000).

We adopt this verbatim. The divisor is the single mechanism that lets the index
absorb **non-market events** — constituent additions/deletions, corporate
actions, share-count or IWF changes — without the level jumping. This is exactly
what a fund needs: rebalances and index maintenance must be invisible to the
benchmark path, otherwise "outperformance" would be an artifact of our own
mechanics.

### 1.2 Multiplicative divisor adjustment (S&P eq. 6)
On an event that changes market value by factor k at constant prices,

    D_new = D_old · k                                            (6)

keeps the level flat across the event. Our `IndexMath.apply_event` implements
exactly this. S&P also offers an additive form (eq. 7, `D_new = D_old +
CMV/IndexLevel`); we use the multiplicative form because it composes cleanly
with chained Fisher arms (see §2.2).

### 1.3 Total-return extension by dividend points
S&P builds a total-return index by converting daily dividends to *index points*
via `IndexDividend = TotalDailyDividend / D`. We borrow the same idea: any
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

    L_p = Σ P_t Q_0 / Σ P_0 Q_0        (Laspeyres price, base shares)
    P_p = Σ P_t Q_t / Σ P_0 Q_t        (Paasche price, current shares)
    F   = √(L_p · P_p)                 (Fisher price = ideal geometric mean)
    V_t = Σ P_t Q_t / D_t              (== S&P value index, by construction)
    Q_F = V_t / F                      (derived Fisher quantity; continuity inherited)

Every variant is stored in one long `index_levels` table keyed by `variant`, and
a `divisors` registry tracks each variant's divisor. This is the structural
improvement: **all constructions are first-class and co-maintained**, so the fund
can run S&P, Laspeyres, Paasche, and Fisher side by side and read the spread
between them as a live signal (e.g. L−P spread = the substitution/drift bias).

**Why this is superior for fund management:** every period return splits
geometrically into a *valuation* component (`ret_price`) and a *capital-structure*
component (`ret_qty`):

    ret_total = ret_price · ret_qty

`ret_qty` isolates share issuance / buyback / float drift — information a
cap-weighted S&P path hides. Fisher also satisfies the time-reversal and
factor-reversal tests that Laspeyres and Paasche each fail individually.

### 2.2 Our parallel divisor methodology: chained (rolling-base) re-anchoring
S&P keeps a *fixed* base (Q_0 from the inception date) and only re-scales via the
divisor at discrete events. Between events, weights drift with price — the well
known **large-cap momentum bias** of cap-weighting (overweighting past winners).

We add a **chained** divisor methodology that re-anchors the base window every
`chain_n` trading days and chains period-over-period *links*:

    link_t = Fisher( basket_t ; basket_{t-1} , base_window )
    level_t = base_level · Π link_τ  (τ ≤ t)

This is the same chaining used in `stock_monitor/fisher_index.py` (and standard
in official Fisher series). Unlike S&P's single divisor, our chained arms carry
*no fixed Q_0* to drift — the base rolls continuously. We maintain **both**
families at once:

  * S&P single divisor D + fixed-base arm divisors (L, P, F) — for exact
    continuity and the Fisher identity `F = √(L·P)`.
  * OUR chained divisors (chained_fisher, chained_laspeyres) — for the
    de-biased path.

The fund can *measure* the bias it is avoiding (live `substitution_bias_ratio`,
`delta_fisher_vs_chained`) rather than assert it.

### 2.3 Divisor events maintained across ALL variants atomically
A non-market event (add/delete, corp action) is absorbed by re-scaling every
maintained divisor at once in `apply_event`:
  * S&P divisor D → D·k (S&P eq. 6), value index flat.
  * fixed arms divisors (L, P, F) → ·k, symmetry preserved.
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

|| Concern | S&P alone | Our addition | Why better for the fund |
|---|---|---|---|
| Continuity across rebalances | ✓ single divisor | same divisor + parallel arm divisors | Non-negotiable; adopted as-is, PLUS all arms co-maintained |
| Price vs quantity separation | ✗ single number | full L/P/Fisher family in parallel | Isolates valuation from capital-structure drift → Fisher quantity sleeve; L−P spread is a live bias gauge |
| Substitution / momentum bias | ✗ fixed base drifts | our chained (rolling-base) divisor | Avoids overweighting past winners; measurable live vs fixed baseline |
| Ideal index properties | ✗ L/P fail tests | Fisher (time/factor reversal) | Theoretically grounded decomposition, co-stored with S&P for audit |
| Quality screen | ✗ out of scope | PIT Buffett/trifecta/leverage | Dual-pass first leg, no look-ahead |
| Non-market event across the family | ✗ chaining can't absorb alone | one `apply_event` re-scales S&P + all arm + chained divisors | Atomic continuity across the whole variant set |
| All constructions at once | ✗ must rebuild | one `run_all`, `index_levels` long table | Compare/blend/stress without re-piping |

**Net:** S&P gives us a correct, industry-standard *continuity primitive*. We
wrap it with a Fisher decomposition and chaining that turn a single cap-weighted
number into a multi-sleeve, regime-aware signal set — directly serving the
fund's design intent: size up only when quality/value, momentum, liquidity, and
regime align, and rotate risk when stress and leadership change.

---

## 4. Pipeline layout (this repo)

| File | Role |
|---|---|
| `src/data/market_data.py` | Capture (trades, prices, shares, sleeves) + PIT fundamentals; audited clean panel; `build_panel_from_parquet` bridges real `stock_monitor` parquet |
| `src/analytics/index_math.py` | `IndexMath`: S&P value index (single divisor) + fixed-base Laspeyres/Paasche/Fisher arms + OUR chained Fisher & chained Laspeyres — **all six maintained in parallel** in `index_levels`; `divisors` registry; `apply_event` re-scales every divisor atomically |
| `src/analytics/quality_value.py` | Buffett/trifecta/leverage/DuPont gates; NULL-safe; reports coverage |
| `src/adapter_stockmonitor.py` | Runs the full pipeline over the real `stock_monitor` parquet store; emits live comparison metrics across the whole variant family |
| `sql/nominal_index_pipeline.sql` | Same math as DuckDB-Wasm SQL for the dashboard SQL Lab |
| `tests/test_index_math.py` | Synthetic property tests: all variants run, Fisher identity `F=√(L·P)`, value = price×quantity, event continuity across all variants, chained ≠ fixed |

See `RUNBOOK.md` for the run/verify commands.
