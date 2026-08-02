"""Smoke + property tests for the parallel multi-variant index math.

All six constructions are computed together and verified:
  - S&P value index uses a single divisor D (continuity on a non-market event)
  - Fisher identity  F  == sqrt(Laspeyres * Paasche)   (fixed base)
  - Value == price * quantity   for every price variant
  - Chained arms: continuity preserved across a divisor (non-market) event,
    and chained != fixed-base (de-biasing active)
  - Our chained_fisher differs from S&P-derived fixed fisher (the improvement)
"""
from __future__ import annotations

import datetime as dt

from src.data.market_data import (
    MarketDataStore, DailyPrice, ShareCount, SleeveTag,
)
from src.analytics import index_math as im


def _synth_store() -> MarketDataStore:
    store = MarketDataStore(":memory:")
    con = store.conn()
    # 3 tickers, 200 days, deterministic so chaining re-anchors a few times
    prices, shares, tags = [], [], []
    base = [100.0, 101.0, 99.0, 102.0, 103.0] * 40
    for i, tk in enumerate(["A", "B", "C"]):
        sh = 1_000_000 * (i + 1)
        for d in range(200):
            date = dt.date(2020, 1, 1) + dt.timedelta(days=d)
            px = base[d]
            prices.append(DailyPrice(tk, date, px, px, px, px, 1_000_000, px))
            shares.append(ShareCount(tk, date, sh, 1.0))
            tags.append(SleeveTag(tk, "SP500", date))
    store.ingest_prices(prices)
    store.ingest_shares(shares)
    store.ingest_sleeves(tags)
    return store


def test_all_variants_run():
    store = _synth_store()
    base = dt.date(2020, 1, 1)
    idx = im.IndexMath(store, base, 1000.0, "SP500", chain_n=21)
    idx.run_all()
    con = store.conn()
    n = con.execute("SELECT COUNT(*) FROM index_levels").fetchone()[0]
    assert n == 200 * len(im.ALL_VARIANTS), n
    # every variant has a final level
    for v in im.ALL_VARIANTS:
        lv = con.execute(
            "SELECT idx FROM index_levels WHERE variant=? "
            "ORDER BY trade_date DESC LIMIT 1", [v]).fetchone()[0]
        assert lv is not None and lv > 0, (v, lv)


def test_fisher_identity_fixed_base():
    """F == sqrt(Laspeyres * Paasche) at every date on the fixed base."""
    store = _synth_store()
    base = dt.date(2020, 1, 1)
    idx = im.IndexMath(store, base, 1000.0, "SP500", chain_n=21)
    idx.run_all()
    con = store.conn()
    bad = con.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT trade_date,
                   MAX(CASE WHEN variant='fisher'    THEN idx END) AS f,
                   MAX(CASE WHEN variant='laspeyres' THEN idx END) AS l,
                   MAX(CASE WHEN variant='paasche'   THEN idx END) AS p
            FROM index_levels GROUP BY trade_date
        ) WHERE ABS(f - SQRT(l*p)) > 1e-6
        """
    ).fetchone()[0]
    assert bad == 0, f"{bad} rows violate Fisher identity"


def test_value_equals_price_times_quantity():
    store = _synth_store()
    base = dt.date(2020, 1, 1)
    idx = im.IndexMath(store, base, 1000.0, "SP500", chain_n=21)
    idx.run_all()
    con = store.conn()
    bad = con.execute(
        """
        WITH q AS (
            SELECT trade_date, variant,
                   nominal_idx / NULLIF(price_idx,0) AS q_implied
            FROM nominal_decomp
        )
        SELECT COUNT(*) FROM q
        WHERE ABS(q_implied - (SELECT qty_idx FROM nominal_decomp x
                               WHERE x.trade_date=q.trade_date
                                 AND x.variant=q.variant)) > 1e-9
        """
    ).fetchone()[0]
    assert bad == 0, bad


def test_event_continuity_all_variants():
    """A non-market event (mv_after != mv_before) must leave every variant's
    level flat AT the event date — the divisor absorbs it. Compare each variant's
    level at the event date before vs after apply_event()."""
    store = _synth_store()
    base = dt.date(2020, 1, 1)
    idx = im.IndexMath(store, base, 1000.0, "SP500", chain_n=21)
    idx.run_all()
    ev = dt.date(2020, 6, 1)
    con = store.conn()
    before = {v: con.execute(
        "SELECT idx FROM index_levels WHERE variant=? AND trade_date=?",
        [v, ev]).fetchone()[0] for v in im.ALL_VARIANTS}
    mv_before = store.conn().execute(
        "SELECT SUM(mv_t) FROM idx_panel WHERE trade_date=?", [ev]).fetchone()[0]
    mv_after = mv_before * 1.05   # +5% non-market event
    idx.apply_event(mv_before, mv_after, ev)
    for v in im.ALL_VARIANTS:
        after = con.execute(
            "SELECT idx FROM index_levels WHERE variant=? AND trade_date=?",
            [v, ev]).fetchone()[0]
        assert before[v] is not None and after is not None
        assert abs(before[v] - after) < 1e-6, (v, before[v], after)


def test_chained_differs_from_fixed():
    """Our chained Fisher must not equal the S&P-derived fixed Fisher base case
    over a horizon long enough to re-anchor -> de-biasing is active."""
    store = _synth_store()
    base = dt.date(2020, 1, 1)
    idx = im.IndexMath(store, base, 1000.0, "SP500", chain_n=21)
    idx.run_all()
    con = store.conn()
    cf = con.execute(
        "SELECT idx FROM index_levels WHERE variant='chained_fisher' "
        "ORDER BY trade_date DESC LIMIT 1").fetchone()[0]
    f = con.execute(
        "SELECT idx FROM index_levels WHERE variant='fisher' "
        "ORDER BY trade_date DESC LIMIT 1").fetchone()[0]
    assert abs(cf - f) > 1e-6, "chained must differ from fixed-base fisher"
    print(f"OK: all variants verified. chained_fisher_last={cf:.2f} "
          f"fisher_last={f:.2f} (differ => de-biasing active)")


if __name__ == "__main__":
    test_all_variants_run()
    test_fisher_identity_fixed_base()
    test_value_equals_price_times_quantity()
    test_event_continuity_all_variants()
    test_chained_differs_from_fixed()
    print("ALL TESTS PASSED")
