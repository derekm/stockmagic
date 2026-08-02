"""Adapter: run the stockmagic parallel-variant index-math pipeline over the
real stock_monitor parquet store, and emit live comparison metrics across the
whole family (S&P value, Laspeyres, Paasche, Fisher, chained Fisher, chained
Laspeyres).

This is the bridge recommended in the analysis: stock_monitor owns the data +
the full analytics suite; stockmagic owns the index-math core (S&P divisor
continuity + the full Fisher/Laspeyres/Paasche/chained family). This module
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
from src.analytics import index_math as im
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

    idx = im.IndexMath(store, base_date, base_level, universe, chain_n=chain_n)
    idx.run_all()

    out: dict = {"variants": {}}
    con = store.conn()
    for v in im.ALL_VARIANTS:
        row = con.execute(
            "SELECT nominal_idx, price_idx, qty_idx, ret_total "
            "FROM nominal_decomp WHERE variant = ? ORDER BY trade_date DESC LIMIT 1",
            [v],
        ).fetchone()
        if row is None:
            continue
        out["variants"][v] = {
            "nominal_last": row[0], "price_last": row[1],
            "qty_last": row[2], "ret_total": row[3],
        }
    out["n_dates"] = con.execute(
        "SELECT COUNT(DISTINCT trade_date) FROM index_levels").fetchone()[0]

    # quality gate (dual-pass first leg) over the latest PIT fundamentals
    pit_as_of = store.conn().execute("SELECT MAX(as_of) FROM pit_fundamentals").fetchone()[0]
    quality_value.qualify(store.conn(), pit_as_of)
    n_pass = store.conn().execute(
        "SELECT COUNT(*) FROM quality_pass "
        "WHERE trifecta_ok AND buffett_ok AND leverage_ok"
    ).fetchone()[0]
    cov = store.conn().execute("SELECT AVG(trifecta_coverage) FROM quality_pass").fetchone()[0]
    out["quality_pass_count"] = n_pass
    out["trifecta_coverage_avg"] = round(cov, 2) if cov is not None else None

    # ---- live comparison metrics across the family -----------------------
    # Pull each variant's level series, join on date, and compare against the
    # S&P value index (the cap-weighted benchmark) and against our chained_fisher.
    out["comparison"] = _family_comparison(con, base_date)

    out["base_date"] = base_date.isoformat()
    out["universe"] = universe
    return out


def _family_comparison(con: duckdb.DuckDBPyConnection, base_date: dt.date) -> dict:
    variants = list(im.ALL_VARIANTS)
    # pivot levels: one column per variant
    cols = ", ".join(
        f"MAX(CASE WHEN variant='{v}' THEN idx END) AS {v}" for v in variants
    )
    rows = con.execute(
        f"SELECT trade_date, {cols} FROM index_levels GROUP BY trade_date ORDER BY trade_date"
    ).fetchall()
    # column index for each variant (1-based, after trade_date)
    colpos = {v: i + 1 for i, v in enumerate(variants)}

    def series(v):
        ci = colpos[v]
        return [(r[0], r[ci]) for r in rows if r[ci] is not None]

    cmp: dict = {}
    # endpoint cumulative returns vs S&P value (cap-weighted benchmark)
    sp_last = dict(series(im.SNP))
    sp0 = sp_last[base_date] if base_date in sp_last else None
    for v in variants:
        s = series(v)
        if not s or sp0 is None:
            continue
        v0 = s[0][1]
        vT = s[-1][1]
        cum = vT / v0 - 1.0
        cum_vs_sp = (vT / sp0 - 1.0) - (sp_last[s[-1][0]] / sp0 - 1.0) if s[-1][0] in sp_last else None
        cmp[v] = {
            "cum_ret": round(cum, 4),
            "cum_ret_vs_sp": round(cum_vs_sp, 4) if cum_vs_sp is not None else None,
            "level_last": round(vT, 2),
        }
    # our chained_fisher vs S&P fixed-base fisher: the substitution-bias delta
    if im.FISHER in cmp and im.CHAINED_FISHER in cmp:
        cmp["delta_fisher_vs_chained"] = round(
            cmp[im.CHAINED_FISHER]["level_last"] - cmp[im.FISHER]["level_last"], 4)
    if im.FISHER in cmp and im.CHAINED_FISHER in cmp:
        f0 = dict(series(im.FISHER))[base_date]
        cf0 = dict(series(im.CHAINED_FISHER))[base_date]
        cmp["substitution_bias_ratio"] = round(
            (dict(series(im.FISHER))[list(sp_last)[-1]] / f0) /
            (dict(series(im.CHAINED_FISHER))[list(sp_last)[-1]] / cf0), 4) \
            if list(sp_last) else None
    return cmp


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
          f"dates={res['n_dates']} "
          f"quality_pass={res['quality_pass_count']} "
          f"trifecta_coverage_avg={res['trifecta_coverage_avg']}")
    for v, r in res["variants"].items():
        print(f"  [{v}] nominal={r['nominal_last']:.1f} "
              f"price={r['price_last']:.1f} qty={r['qty_last']:.4f} "
              f"ret_total={r['ret_total']:.4f}")
    print("FAMILY COMPARISON (cumulative return vs S&P value benchmark):")
    for v, c in res["comparison"].items():
        if v in ("delta_fisher_vs_chained", "substitution_bias_ratio"):
            continue
        print(f"  {v:20s} cum_ret={c['cum_ret']:+.4f} "
              f"vs_SP={c['cum_ret_vs_sp']:+.4f} level={c['level_last']}")
    if "delta_fisher_vs_chained" in res["comparison"]:
        print(f"  delta_fisher_vs_chained(level pts)="
              f"{res['comparison']['delta_fisher_vs_chained']}")
    if "substitution_bias_ratio" in res["comparison"]:
        print(f"  substitution_bias_ratio="
              f"{res['comparison']['substitution_bias_ratio']}")


if __name__ == "__main__":
    main()
