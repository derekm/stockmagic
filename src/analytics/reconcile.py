"""Reconcile stockmagic's index family against stock_monitor's own Fisher output.

Missing link #2 (from MISSING_LINKS.md): stock_monitor already computes a Fisher
price index (`fisher_indexes.parquet` from `fisher_index.py`) over the SAME
underlying data stockmagic bridges in via `build_panel_from_parquet`.

IMPORTANT (verified from source): stock_monitor's `fisher_p` / `laspeyres_p` /
`paasche_p` are ALL **chained** (cumulative product of period links, base 100) and
use **volume** as the quantity. stockmagic's `sp_*` variants instead use shares x
iwf and a fixed base; they are NOT the stock_monitor method. So the like-for-like
reconciliation is stock_monitor (chained, volume) vs stockmagic's `vol_chained_*`
variants (also chained, also volume) — the only intended differences are the base
level (100 vs 1000, cancels in `norm`) and the link window (stock_monitor links
t-1 -> t; stockmagic uses a rolling 63-trading-day base = the de-biasing it owns).

NOTE: that 63-trading-day rolling base is stockmagic's OWN chained-index
choice (a substitution-bias reduction in the C-CPI-U family) — it is NOT S&P
methodology. S&P 500 uses a FIXED 1941–43=10 base with a divisor that only
adjusts on corporate actions; there is no periodic rebase or period-link
chaining in the S&P level. The full S&P-vs-stockmagic contrast is documented
in `src/analytics/index_math.py` (module docstring, "S&P 500 technique vs OUR
rolling rebase"). The ~<1% norm residual on Fisher/Laspeyres IS this
base-window difference and is the expected, correct gap.

stock_monitor `fisher_indexes_sp500.parquet` columns used here (the SP500
sleeve — necessary so the universes match stockmagic's SP500 panel):
    date          DATE       (daily date-key, no time component)
    fisher_p      DOUBLE     (chained Fisher price index, volume Q, base 100)
    laspeyres_p   DOUBLE     (chained Laspeyres, volume Q)
    paasche_p     DOUBLE     (chained Paasche, volume Q)

stockmagic `index_levels` rows used (the chained, volume-Q twins):
    variant='vol_chained_fisher' | 'vol_chained_laspeyres' | 'vol_chained_paasche'
"""

from __future__ import annotations

import datetime as dt

import duckdb


# stock_monitor fisher_indexes.parquet column -> stockmagic index_levels variant.
# Every pair here is chained + volume-Q on BOTH sides (like-for-like).
_VOL_CHAINED_MAP = {
    "fisher_p": "vol_chained_fisher",
    "laspeyres_p": "vol_chained_laspeyres",
    "paasche_p": "vol_chained_paasche",
}


def reconcile_fisher(store, stockmonitor_dir: str,
                     stockmonitor_fisher: str = "fisher_indexes_sp500.parquet") -> dict:
    """Compare stock_monitor's chained Fisher output to stockmagic's vol_chained_*.

    IMPORTANT: the comparison is only meaningful when BOTH sides cover the same
    ticker universe. stock_monitor's default `fisher_indexes.parquet` is the
    `portfolio` (8-name) basket and CANNOT validate stockmagic's SP500 index.
    The default here is `fisher_indexes_sp500.parquet` — stock_monitor's Fisher
    index computed over the SAME sp500_member sleeve stockmagic bridges.

    `date` is DATE in the parquet and `index_levels.trade_date` is DATE, so the
    join is a clean DATE = DATE (no cast).

    Returns a dict keyed by stock_monitor column, each with overlap stats.

    Returns a dict keyed by stock_monitor column, each with:
        n_overlap       -- number of dates compared
        max_abs_pct     -- max |sm - smm| / sm  across the overlapping window
                          (RAW, including any base-scaling difference)
        max_abs_pct_norm -- same, but AFTER both series are re-based to 100 at
                          the first overlapping date. This isolates the REAL
                          divergence (link-window choice) from the trivial
                          base-scaling (stock_monitor base 100 vs
                          stockmagic base 1000).
        last_sm / last_smm -- final levels on each side (for eyeballing)

    Raises FileNotFoundError if the stock_monitor parquet is absent (caller decides
    whether reconciliation is optional).
    """
    con = store.conn()
    path = f"{stockmonitor_dir}/{stockmonitor_fisher}"
    # `date` is DATE in the parquet, index_levels.trade_date is DATE -> clean
    # DATE=DATE join, no cast.
    con.execute(
        f"CREATE OR REPLACE TABLE sm_fisher AS "
        f"SELECT date AS d, fisher_p, laspeyres_p, paasche_p "
        f"FROM read_parquet('{path}')"
    )

    out: dict = {}
    for col, variant in _VOL_CHAINED_MAP.items():
        row = con.execute(
            f"""
            WITH j AS (
                SELECT s.{col} AS sm, m.idx AS smm,
                       FIRST(s.{col}) OVER (ORDER BY s.d) AS sm0,
                       FIRST(m.idx)  OVER (ORDER BY s.d) AS smm0
                FROM sm_fisher s
                JOIN index_levels m
                  ON m.trade_date = s.d AND m.variant = '{variant}'
            )
            SELECT
                COUNT(*),
                MAX(ABS(sm - smm) / NULLIF(sm, 0)),
                MAX(ABS(sm/sm0 - smm/smm0) / NULLIF(sm/sm0, 0)),
                MAX(sm), MAX(smm)
            FROM j
            """
        ).fetchone()
        out[col] = {
            "variant": variant,
            "n_overlap": row[0],
            "max_abs_pct": round(row[1], 6) if row[1] is not None else None,
            "max_abs_pct_norm": round(row[2], 6) if row[2] is not None else None,
            "last_sm": round(row[3], 2) if row[3] is not None else None,
            "last_smm": round(row[4], 2) if row[4] is not None else None,
        }
    return out


def format_reconcile(r: dict) -> str:
    lines = ["FISHER RECONCILIATION (stockmagic vol_chained_* vs stock_monitor",
             "  fisher_indexes_sp500.parquet — SAME sp500_member sleeve):",
             "  (both sides chained + volume-Q; norm re-based to 100 at overlap start)",
             "  max_abs_pct = raw; max_abs_pct_norm = link-window divergence only"]
    for k, v in r.items():
        lines.append(
            f"  {k:14s} -> {v['variant']:22s} n={v['n_overlap']:5d}  "
            f"raw={v['max_abs_pct']}  norm={v['max_abs_pct_norm']}  "
            f"sm_last={v['last_sm']}  smm_last={v['last_smm']}"
        )
    return "\n".join(lines)
