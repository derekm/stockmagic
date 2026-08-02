"""Quality / value factor gates for the dual-pass screen.

Mirrors the suite spec:
  - Buffett ROE/ROIC
  - Trifecta:  EV/EBITDA <= 9,  P/B <= 1.5,  MktCap/Assets <= 0.5
  - Leverage flags (D/E, interest coverage)
  - DuPont decomposition (margin x turnover x leverage)
  - Live thresholds (moving-average momentum gate)
"""
from __future__ import annotations

import datetime as dt

import duckdb


TRIFECTA = {"ev_ebitda": 9.0, "pb": 1.5, "mcap_assets": 0.5}


def qualify(conn: duckdb.DuckDBPyConnection, as_of: dt.date) -> None:
    """Build `quality_pass` table: one row per ticker for the quality gate.
    PIT fundamentals joined to the latest available before `as_of`.

    NULL handling: a missing metric is treated as "unknown" and does NOT
    disqualify the stock (so a missing EV/EBITDA doesn't veto an otherwise
    cheap name). `trifecta_coverage` counts how many of the three legs were
    actually observed, so callers can see screening quality."""
    conn.execute(
        """
        CREATE OR REPLACE TABLE quality_pass AS
        WITH f AS (
            SELECT * FROM pit_fundamentals f1
            WHERE f1.as_of = (
                SELECT MAX(as_of) FROM pit_fundamentals f2
                WHERE f2.ticker = f1.ticker AND f2.as_of <= ?)
        )
        SELECT
            f.ticker,
            f.roe, f.roic,
            f.ev_ebitda, f.pb, f.mcap_assets,
            f.debt_equity, f.interest_coverage,
            -- each leg: NULL => unknown (passes); present => must meet threshold
            (f.ev_ebitda IS NULL OR f.ev_ebitda <= ?)                       AS ev_ok,
            (f.pb         IS NULL OR f.pb         <= ?)                      AS pb_ok,
            (f.mcap_assets IS NULL OR f.mcap_assets <= ?)                    AS mca_ok,
            ((f.ev_ebitda IS NULL OR f.ev_ebitda <= ?)
             AND (f.pb IS NULL OR f.pb <= ?)
             AND (f.mcap_assets IS NULL OR f.mcap_assets <= ?))              AS trifecta_ok,
            (CASE WHEN f.ev_ebitda IS NOT NULL THEN 1 ELSE 0 END
             + CASE WHEN f.pb IS NOT NULL THEN 1 ELSE 0 END
             + CASE WHEN f.mcap_assets IS NOT NULL THEN 1 ELSE 0 END)       AS trifecta_coverage,
            (f.roe >= 0.15 AND f.roic >= 0.10)                              AS buffett_ok,
            (f.debt_equity <= 2.0 AND f.interest_coverage >= 1.5)           AS leverage_ok
        FROM f
        """,
        [as_of, TRIFECTA["ev_ebitda"], TRIFECTA["pb"], TRIFECTA["mcap_assets"],
         TRIFECTA["ev_ebitda"], TRIFECTA["pb"], TRIFECTA["mcap_assets"]],
    )


def dupont(conn: duckdb.DuckDBPyConnection) -> None:
    """ROE = margin * turnover * leverage. Surfaces which leg is weak."""
    conn.execute(
        """
        CREATE OR REPLACE TABLE dupont AS
        SELECT ticker,
            net_margin * asset_turnover * equity_mult            AS roe_implied,
            net_margin, asset_turnover, equity_mult
        FROM pit_fundamentals
        """
    )
