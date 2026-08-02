"""Smoke test: build a tiny universe, run the index math, assert identities.

Uses synthetic data so it runs with no external dependencies beyond duckdb.
"""
from __future__ import annotations

import datetime as dt

from src.data.market_data import (
    MarketDataStore, RawTrade, DailyPrice, ShareCount, SleeveTag,
)
from src.analytics.index_math import IndexMath


def _seed(store: MarketDataStore) -> None:
    base = dt.date(2024, 1, 1)
    days = [base + dt.timedelta(days=i) for i in range(5)]
    tickers = ["AAA", "BBB"]
    # prices drift up 1% per day; 100 shares each, IWF 1.0
    for t in tickers:
        pr = []
        for i, d in enumerate(days):
            px = 100.0 * (1.01 ** i)
            pr.append(DailyPrice(t, d, px, px, px, px, 1000, px))
        store.ingest_prices(pr)
        store.ingest_shares([ShareCount(t, base, 100.0, 1.0)])
        store.ingest_sleeves([SleeveTag(t, "SP500", base)])


def test_value_and_fisher_identity():
    store = MarketDataStore(":memory:")
    _seed(store)
    idx = IndexMath(store, dt.date(2024, 1, 1), base_level=1000.0)
    idx.run_all()

    con = store.conn()
    rows = con.execute(
        "SELECT trade_date, nominal_idx, price_idx, qty_idx FROM nominal_decomp "
        "ORDER BY trade_date").fetchall()
    assert len(rows) == 5

    # Identity check: nominal == price * qty within float epsilon
    for _, nominal, price, qty in rows:
        assert abs(nominal - price * qty) < 1e-6, f"identity broken: {nominal} vs {price*qty}"

    # Continuity across a non-market event: divisor keeps level unchanged.
    # A real event changes the panel's MV at the event date (e.g. a constituent
    # swap). We mutate idx_panel so MV at 2024-01-03 grows 10%, then the divisor
    # adjustment must restore the level to ~1000 at that date.
    ev = dt.date(2024, 1, 3)
    # level just before the event (natural drift, no jump yet)
    lvl_before = con.execute(
        "SELECT value_idx FROM value_index WHERE trade_date=?", [ev]).fetchone()[0]
    mv_before = con.execute(
        "SELECT SUM(mv_t) FROM idx_panel WHERE trade_date=?", [ev]).fetchone()[0]
    con.execute(
        "UPDATE idx_panel SET mv_t = mv_t * 1.10 WHERE trade_date >= ?", [ev])
    mv_after = mv_before * 1.10
    k = idx.apply_event(mv_before, mv_after, ev)
    assert abs(k - 1.10) < 1e-9
    # continuity: the level must NOT jump across the event
    lvl_after = con.execute(
        "SELECT value_idx FROM value_index WHERE trade_date=?",
        [ev]).fetchone()[0]
    assert abs(lvl_after - lvl_before) < 1e-6, \
        f"continuity broken: {lvl_before} -> {lvl_after}"

    print("OK: value/Fisher identity + divisor continuity verified")


if __name__ == "__main__":
    test_value_and_fisher_identity()
