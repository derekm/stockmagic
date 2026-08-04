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
        chain_n: int = 63, start_year: int | None = None,
        reconcile: bool = False) -> dict:
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

    out: dict = {"variants": {}}
    con = store.conn()

    # PIT snapshot timeseries + backfill (daily, forward-filled, PIT-honest)
    from src.data import pit_snapshots as pits
    n_snap = pits.build_snapshot_timeseries(store, recompute_marketcap=True)
    out["pit_snapshots"] = n_snap

    idx = im.IndexMath(store, base_date, base_level, universe, chain_n=chain_n)
    idx.run_all()

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

    # quality gate (dual-pass first leg) over the PIT snapshot timeseries.
    # Report the latest snapshot's pass count, then a HISTORICAL sweep: qualify
    # as-of each year-end to show how the quality set evolves (no look-ahead).
    pit_latest = store.conn().execute(
        "SELECT MAX(snapshot_date) FROM pit_snapshots").fetchone()[0]
    pits.qualify_as_of(store, pit_latest)
    n_pass = store.conn().execute(
        "SELECT COUNT(*) FROM quality_pass "
        "WHERE trifecta_ok AND buffett_ok AND leverage_ok"
    ).fetchone()[0]
    cov = store.conn().execute("SELECT AVG(trifecta_coverage) FROM quality_pass").fetchone()[0]
    out["quality_pass_count"] = n_pass
    out["trifecta_coverage_avg"] = round(cov, 2) if cov is not None else None
    # historical sweep: qualify as-of each year-end to show how the quality
    # set evolves (no look-ahead — qualify_as_of picks the latest PIT snapshot
    # that is <= the year-end date).
    year_ends = store.conn().execute(
        "SELECT DISTINCT LAST_DAY(DATE_TRUNC('year', trade_date) + INTERVAL 1 YEAR - INTERVAL 1 DAY) AS ye "
        "FROM daily_prices ORDER BY ye"
    ).fetchall()
    yearly = []
    for (sd,) in year_ends:
        pits.qualify_as_of(store, sd)
        np_ = store.conn().execute(
            "SELECT COUNT(*) FROM quality_pass "
            "WHERE trifecta_ok AND buffett_ok AND leverage_ok"
        ).fetchone()[0]
        yearly.append({"as_of": sd.isoformat(), "quality_pass": np_})
    out["quality_sweep_yearly"] = yearly

    # ---- live comparison metrics across the family -----------------------
    # Pull each variant's level series, join on date, and compare against the
    # S&P value index (the cap-weighted benchmark) and against our chained_fisher.
    out["comparison"] = _family_comparison(con, base_date)

    # ---- OPTIONAL: reconcile against stock_monitor's own Fisher output -----
    if reconcile:
        from src.analytics import reconcile as rec
        try:
            rec_tbl = rec.reconcile_fisher(store, data_dir)
            out["reconcile_fisher"] = rec_tbl
        except Exception as e:  # file missing / LFS stub — reconciliation optional
            out["reconcile_fisher_error"] = str(e)

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
    sp_last = dict(series("sp_value"))
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
    if "sp_fisher" in cmp and "sp_chained_fisher" in cmp:
        cmp["delta_fisher_vs_chained"] = round(
            cmp["sp_chained_fisher"]["level_last"] - cmp["sp_fisher"]["level_last"], 4)
    if "sp_fisher" in cmp and "sp_chained_fisher" in cmp:
        f0 = dict(series("sp_fisher"))[base_date]
        cf0 = dict(series("sp_chained_fisher"))[base_date]
        cmp["substitution_bias_ratio"] = round(
            (dict(series("sp_fisher"))[list(sp_last)[-1]] / f0) /
            (dict(series("sp_chained_fisher"))[list(sp_last)[-1]] / cf0), 4) \
            if list(sp_last)[-1] in sp_last else None
    return cmp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="../stock_monitor")
    ap.add_argument("--universe", default="SP500")
    ap.add_argument("--base-level", type=float, default=1000.0)
    ap.add_argument("--start-year", type=int, default=2015,
                    help="cap history to start at this year (memory guard)")
    ap.add_argument("--chain-n", type=int, default=63)
    ap.add_argument("--reconcile", action="store_true",
                    help="compare stockmagic index_levels against stock_monitor "
                         "fisher_indexes.parquet (validates the reimplementation)")
    args = ap.parse_args()

    res = run(args.data_dir, args.universe, args.base_level, args.chain_n,
             args.start_year, reconcile=args.reconcile)
    print(f"Universe={res['universe']} base={res['base_date']} "
          f"dates={res['n_dates']} "
          f"pit_snapshots={res['pit_snapshots']} "
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
    print("QUALITY SWEEP (quality_pass count as-of each year-end):")
    for y in res.get("quality_sweep_yearly", []):
        print(f"  {y['as_of']}  quality_pass={y['quality_pass']}")
    if "reconcile_fisher" in res:
        from src.analytics import reconcile as rec
        print(rec.format_reconcile(res["reconcile_fisher"]))
    elif "reconcile_fisher_error" in res:
        print(f"RECONCILE SKIPPED: {res['reconcile_fisher_error']}")


if __name__ == "__main__":
    main()
