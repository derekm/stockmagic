"""Adapter: run the stockmagic index-math pipeline over the real
stock_monitor parquet store, and compare the chained (innovation) vs
fixed-base (S&P-like) Fisher decompositions.

This is the bridge recommended in the analysis: stock_monitor already has the
data + the full analytics suite (regime, stress dual-pass, etc.); stockmagic
owns the index-math core (S&P divisor continuity + chained Fisher). This module
wires the two together using the real parquet files on disk.

Usage:
    python -m src.adapter_stockmonitor --data-dir ../stock_monitor --universe SP500
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib

import duckdb

from src.data.market_data import MarketDataStore
from src.analytics.index_math import IndexMath, CHAINED_MODE, FIXED_MODE
from src.analytics import quality_value


def run(data_dir: str, universe: str, base_level: float = 1000.0,
        chain_n: int = 63, start_year: int | None = None) -> dict:
    store = MarketDataStore(":memory:")
    # base_date = first trading day >= start_year (snap to actual price dates
    # so the divisor's base-day SUM is never empty).
    first_price = store.conn().execute(
        f"SELECT MIN(date) FROM read_parquet('{data_dir}/daily_prices_clean.parquet')"
    ).fetchone()[0]
    base_date = first_price
    if start_year:
        snapped = store.conn().execute(
            f"SELECT MIN(date) FROM read_parquet('{data_dir}/daily_prices_clean.parquet') "
            f"WHERE date >= DATE '{start_year}-01-01'"
        ).fetchone()[0]
        base_date = snapped if snapped else first_price

    store.build_panel_from_parquet(data_dir, base_date, universe)

    out = {}
    for mode in (CHAINED_MODE, FIXED_MODE):
        idx = IndexMath(store, base_date, base_level, universe,
                        chain_n=chain_n, mode=mode)
        idx.run_all()
        con = store.conn()
        last = con.execute(
            "SELECT nominal_idx, price_idx, qty_idx, ret_total, ret_price, ret_qty "
            "FROM nominal_decomp ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        out[mode] = {
            "n_dates": con.execute("SELECT COUNT(*) FROM nominal_decomp").fetchone()[0],
            "nominal_last": last[0], "price_last": last[1], "qty_last": last[2],
            "ret_total": last[3], "ret_price": last[4], "ret_qty": last[5],
        }
        # stash tables under a mode prefix so the dashboard can read both
        for tbl in ("value_index", "fisher_price", "fisher_qty", "nominal_decomp"):
            con.execute(f"CREATE OR REPLACE TABLE {tbl}_{mode} AS SELECT * FROM {tbl}")

    # quality gate (dual-pass first leg) over the latest PIT fundamentals
    # snapshot (independent of the index base_date, which may predate all PIT data)
    pit_as_of = store.conn().execute(
        "SELECT MAX(as_of) FROM pit_fundamentals"
    ).fetchone()[0]
    quality_value.qualify(store.conn(), pit_as_of)
    n_pass = store.conn().execute(
        "SELECT COUNT(*) FROM quality_pass "
        "WHERE trifecta_ok AND buffett_ok AND leverage_ok"
    ).fetchone()[0]
    cov = store.conn().execute(
        "SELECT AVG(trifecta_coverage) FROM quality_pass"
    ).fetchone()[0]
    out["quality_pass_count"] = n_pass
    out["trifecta_coverage_avg"] = round(cov, 2) if cov is not None else None

    # ---- live comparison metrics: chained (our innovation) vs fixed (S&P) ----
    con = store.conn()
    cmp_rows = con.execute(
        """
        SELECT c.nominal_idx, f.nominal_idx,
               c.qty_idx,     f.qty_idx,
               c.ret_total,   f.ret_total
        FROM nominal_decomp_chained c
        JOIN nominal_decomp_fixed   f USING (trade_date)
        ORDER BY c.trade_date
        """
    ).fetchall()
    if cmp_rows:
        n0_c, n0_f = cmp_rows[0][0], cmp_rows[0][1]
        nT_c, nT_f = cmp_rows[-1][0], cmp_rows[-1][1]
        cum_ret_c = nT_c / n0_c - 1.0
        cum_ret_f = nT_f / n0_f - 1.0
        # live path tracking error: max |cum_ret_c(t) - cum_ret_f(t)| across path
        max_div = max(abs(rc / n0_c - 1.0 - (rf / n0_f - 1.0))
                      for rc, rf, *_ in cmp_rows)
        # bias ratio: how much the fixed (S&P) base over-/under-states the
        # quantity (capital-structure) arm vs the chained ideal.
        qT_c, qT_f = cmp_rows[-1][2], cmp_rows[-1][3]
        bias_ratio = qT_f / qT_c if qT_c else None
        # price-arm (Fisher) divergence: where chaining actually changes the path
        fp = con.execute(
            "SELECT c.fisher_price_idx, f.fisher_price_idx "
            "FROM fisher_price_chained c JOIN fisher_price_fixed f USING (trade_date) "
            "ORDER BY c.trade_date"
        ).fetchall()
        pT_c, pT_f = fp[-1][0], fp[-1][1]
        out["comparison"] = {
            "cum_ret_chained": round(cum_ret_c, 4),
            "cum_ret_fixed": round(cum_ret_f, 4),
            "cum_ret_differential": round(cum_ret_c - cum_ret_f, 4),
            "qty_chain_last": round(qT_c, 4),
            "qty_fixed_last": round(qT_f, 4),
            "substitution_bias_ratio": round(bias_ratio, 4) if bias_ratio else None,
            "price_chain_last": round(pT_c, 4),
            "price_fixed_last": round(pT_f, 4),
            "fisher_price_divergence": round(pT_c - pT_f, 4),
            "max_path_divergence": round(max_div, 4),
            "n_overlap_dates": len(cmp_rows),
        }
    else:
        out["comparison"] = {}

    out["base_date"] = base_date.isoformat()
    out["universe"] = universe
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="../stock_monitor")
    ap.add_argument("--universe", default="SP500")
    ap.add_argument("--base-level", type=float, default=1000.0)
    ap.add_argument("--start-year", type=int, default=2015,
                     help="cap history to start at this year (memory guard)")
    ap.add_argument("--chain-n", type=int, default=63)
    args = ap.parse_args()

    res = run(args.data_dir, args.universe, args.base_level, args.chain_n,
             args.start_year)
    print(f"Universe={res['universe']} base={res['base_date']} "
          f"dates={res[CHAINED_MODE]['n_dates']} "
          f"quality_pass={res['quality_pass_count']} "
          f"trifecta_coverage_avg={res['trifecta_coverage_avg']}")
    for mode in (CHAINED_MODE, FIXED_MODE):
        r = res[mode]
        print(f"  [{mode}] nominal={r['nominal_last']:.1f} "
              f"price={r['price_last']:.1f} qty={r['qty_last']:.4f} "
              f"ret_total={r['ret_total']:.4f} "
              f"(price {r['ret_price']:.4f} / qty {r['ret_qty']:.4f})")
    print("Tables available: value_index_chained, fisher_price_chained, "
          "nominal_decomp_fixed, quality_pass, ...")
    c = res["comparison"]
    if c:
        print("COMPARISON chained-vs-S&P-fixed:")
        print(f"  cum_ret_chained={c['cum_ret_chained']:.4f} "
              f"cum_ret_fixed={c['cum_ret_fixed']:.4f} "
              f"differential={c['cum_ret_differential']:+.4f}")
        print(f"  qty_chain_last={c['qty_chain_last']} "
              f"qty_fixed_last={c['qty_fixed_last']} "
              f"substitution_bias_ratio={c['substitution_bias_ratio']} "
              f"fisher_price_divergence={c['fisher_price_divergence']} "
              f"(overlap_dates={c['n_overlap_dates']})")


if __name__ == "__main__":
    main()
