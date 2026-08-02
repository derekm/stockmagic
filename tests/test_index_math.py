"""Smoke + property tests for the index math.

Covers both modes so we can compare the innovation (chained) against the
S&P-like baseline (fixed):
  - Fisher identity  nominal == price * qty  (every date, both modes)
  - Chaining produces finite, positive levels
  - Divisor event preserves value-index continuity (no jump) in both modes
"""
from __future__ import annotations

import datetime as dt

from src.data.market_data import (
    MarketDataStore, DailyPrice, ShareCount, SleeveTag,
)
from src.analytics.index_math import IndexMath, FIXED_MODE, CHAINED_MODE


def _seed(store: MarketDataStore, n_days: int = 200) -> None:
    base = dt.date(2024, 1, 1)
    days = [base + dt.timedelta(days=i) for i in range(n_days)]
    for t in ["AAA", "BBB", "CCC"]:
        pr = []
        for i, d in enumerate(days):
            px = 100.0 * (1.004 ** i) * (1.0 + 0.001 * ((i * 7) % 5))
            pr.append(DailyPrice(t, d, px, px, px, px, 1000 + i, px))
        store.ingest_prices(pr)
        store.ingest_shares([ShareCount(t, base, 100.0 + 0.1 * i, 1.0)])
        store.ingest_sleeves([SleeveTag(t, "SP500", base)])


def _check_mode(mode: str, n_days: int = 200):
    store = MarketDataStore(":memory:")
    _seed(store, n_days)
    idx = IndexMath(store, dt.date(2024, 1, 1), base_level=1000.0,
                    chain_n=21, mode=mode)
    idx.run_all()
    con = store.conn()

    rows = con.execute(
        "SELECT trade_date, nominal_idx, price_idx, qty_idx FROM nominal_decomp "
        "ORDER BY trade_date").fetchall()
    assert len(rows) == n_days, f"{mode}: expected {n_days} rows, got {len(rows)}"

    for _, nominal, price, qty in rows:
        assert price > 0 and qty > 0, f"{mode}: non-positive Fisher level"
        assert abs(nominal - price * qty) < 1e-6, \
            f"{mode}: identity broken {nominal} vs {price*qty}"

    # continuity across a non-market event
    ev = dt.date(2024, 2, 1)
    lvl_before = con.execute(
        "SELECT value_idx FROM value_index WHERE trade_date=?", [ev]).fetchone()[0]
    mv_before = con.execute(
        "SELECT SUM(mv_t) FROM idx_panel WHERE trade_date>=?", [ev]).fetchone()[0]
    con.execute("UPDATE idx_panel SET mv_t = mv_t * 1.10 WHERE trade_date >= ?", [ev])
    k = idx.apply_event(mv_before, mv_before * 1.10, ev)
    assert abs(k - 1.10) < 1e-9
    lvl_after = con.execute(
        "SELECT value_idx FROM value_index WHERE trade_date=?", [ev]).fetchone()[0]
    assert abs(lvl_after - lvl_before) < 1e-6, \
        f"{mode}: continuity broken {lvl_before} -> {lvl_after}"
    return idx


def test_both_modes():
    chained = _check_mode(CHAINED_MODE)
    fixed = _check_mode(FIXED_MODE)

    # The two modes should differ (chaining de-biases vs fixed base) but both
    # stay positive and finite. We don't assert which is "better" here; the
    # dashboard comparison is the place for that judgement.
    c_last = chained.con.execute(
        "SELECT fisher_price_idx FROM fisher_price ORDER BY trade_date DESC LIMIT 1").fetchone()[0]
    f_last = fixed.con.execute(
        "SELECT fisher_price_idx FROM fisher_price ORDER BY trade_date DESC LIMIT 1").fetchone()[0]
    assert c_last > 0 and f_last > 0
    print(f"OK: both modes verified. chained_price_last={c_last:.2f} "
          f"fixed_price_last={f_last:.2f} (differ => de-biasing active)")


if __name__ == "__main__":
    test_both_modes()
