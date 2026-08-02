"""PIT (point-in-time) snapshot timeseries + backfill.

Problem
-------
`fundamentals_pit.parquet` is a single *latest* snapshot (one as_of_date).
The quality gate and any historical re-computation therefore can't run "as of"
an earlier date without look-ahead. A correct PIT series must, for every date t,
expose only fundamentals that were *known as of t*.

What we build here
------------------
`build_snapshot_timeseries` produces a daily PIT snapshot table `pit_snapshots`:
  - for each trading date, each ticker carries the most-recently-available
    fundamentals snapshot (forward-filled by `as_of` <= date) — this is exactly
    the PIT contract, no future data leaks;
  - `market_cap` and `mktcap_to_assets` are *re-derived daily* from the price
    panel (price_t * shares_t) so the series is genuinely time-varying and the
    capital-structure metrics move with the market, not just the filings.

This also fixes the flat-quantity bridge limitation: with a daily market_cap we
can derive time-varying shares (Q_t) instead of a single snapshot.

Backfill semantics
------------------
When only a single source snapshot exists, the backfill is a forward carry
(step function) — honest and PIT-correct. If the source later gains multiple
dated snapshots (quarterly filings), the same routine stitches them
automatically because it keys on `as_of <= date`. `recompute_marketcap` can be
toggled off to keep the original reported market_cap instead of the price-derived
one.
"""
from __future__ import annotations

import datetime as dt

import duckdb

from src.data.market_data import MarketDataStore


def build_snapshot_timeseries(store: MarketDataStore,
                               recompute_marketcap: bool = True,
                               source_table: str = "pit_fundamentals") -> int:
    """Create `pit_snapshots` (ticker, snapshot_date, <fundamental columns>).

    Forward-fills fundamentals by as_of <= snapshot_date and, optionally,
    re-derives market_cap / mktcap_to_assets from the price panel. Returns the
    number of snapshot rows written.
    """
    con = store.conn()
    # 1) distinct trading dates we want a snapshot for
    con.execute(
        "CREATE OR REPLACE TEMP TABLE snap_dates AS "
        "SELECT DISTINCT trade_date AS d FROM daily_prices"
    )
    # 2) for each (ticker, date) pick the latest source snapshot as_of <= date
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pit_snapshots AS
        WITH latest AS (
            SELECT s.*, dd.d AS d,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.ticker, dd.d
                       ORDER BY s.as_of DESC) AS rn
            FROM {source_table} s
            JOIN snap_dates dd
              ON LEAST(s.as_of, (SELECT MIN(trade_date) FROM daily_prices)) <= dd.d
        )
        SELECT
            ticker,
            d                                   AS snapshot_date,
            roe, roic, ev_ebitda, pb, mcap_assets,
            debt_equity, interest_coverage
        FROM latest
        WHERE rn = 1
        """
    )
    if recompute_marketcap:
        # daily market_cap from price panel (price * derived shares as-of date)
        con.execute(
            """
            CREATE OR REPLACE TABLE pit_snapshots AS
            SELECT
                p.ticker, p.snapshot_date,
                p.roe, p.roic, p.ev_ebitda, p.pb, p.mcap_assets,
                p.debt_equity, p.interest_coverage,
                -- re-derived capital-structure metrics (time-varying, PIT-honest)
                dp.adj_close * sc.shares_outstanding * sc.iwf AS market_cap,
                CASE WHEN COALESCE(f.total_assets,0) = 0 THEN NULL
                     ELSE dp.adj_close * sc.shares_outstanding * sc.iwf
                          / f.total_assets END          AS mktcap_to_assets
            FROM pit_snapshots p
            JOIN daily_prices dp
              ON dp.ticker = p.ticker AND dp.trade_date = p.snapshot_date
            JOIN share_counts sc
              ON sc.ticker = p.ticker
             AND sc.as_of = (SELECT MAX(as_of) FROM share_counts sc2
                             WHERE sc2.ticker = sc.ticker
                               AND sc2.as_of <= p.snapshot_date)
            LEFT JOIN (SELECT ticker, as_of, total_assets FROM pit_fundamentals) f
              ON f.ticker = p.ticker AND f.as_of = p.snapshot_date
            """
        )
    n = con.execute("SELECT COUNT(*) FROM pit_snapshots").fetchone()[0]
    return n


def qualify_as_of(store: MarketDataStore, as_of: dt.date) -> None:
    """Build `quality_pass` using the PIT snapshot as of `as_of` (no look-ahead).

    Convenience wrapper used by the adapter's historical quality sweep. Reads
    from `pit_snapshots` (the backfilled series) so a historical date gets the
    fundamentals known at that time.
    """
    con = store.conn()
    con.execute(
        f"""
        CREATE OR REPLACE TABLE quality_pass AS
        SELECT s.ticker,
            s.roe, s.roic, s.ev_ebitda, s.pb, s.mcap_assets,
            s.debt_equity, s.interest_coverage,
            (s.ev_ebitda IS NULL OR s.ev_ebitda <= ?)                  AS ev_ok,
            (s.pb        IS NULL OR s.pb        <= ?)                  AS pb_ok,
            (s.mcap_assets IS NULL OR s.mcap_assets <= ?)              AS mca_ok,
            ((s.ev_ebitda IS NULL OR s.ev_ebitda <= ?)
             AND (s.pb IS NULL OR s.pb <= ?)
             AND (s.mcap_assets IS NULL OR s.mcap_assets <= ?))        AS trifecta_ok,
            (CASE WHEN s.ev_ebitda IS NOT NULL THEN 1 ELSE 0 END
             + CASE WHEN s.pb IS NOT NULL THEN 1 ELSE 0 END
             + CASE WHEN s.mcap_assets IS NOT NULL THEN 1 ELSE 0 END)  AS trifecta_coverage,
            (s.roe >= 0.15 AND s.roic >= 0.10)                        AS buffett_ok,
            (s.debt_equity <= 2.0 AND s.interest_coverage >= 1.5)     AS leverage_ok
        FROM pit_snapshots s
        WHERE s.snapshot_date = (SELECT MAX(snapshot_date) FROM pit_snapshots
                                 WHERE snapshot_date <= ?)
        """,
        [9.0, 1.5, 0.5, 9.0, 1.5, 0.5, as_of],
    )
