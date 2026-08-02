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
    """Build `quality_pass` table: one boolean per ticker for the quality gate.
    PIT fundamentals joined to the latest available before `as_of`."""
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
            (f.ev_ebitda <= ? AND f.pb <= ? AND f.mcap_assets <= ?) AS trifecta_ok,
            (f.roe >= 0.15 AND f.roic >= 0.10)                       AS buffett_ok,
            (f.debt_equity <= 2.0 AND f.interest_coverage >= 1.5)    AS leverage_ok
        FROM f
        """,
        [as_of, TRIFECTA["ev_ebitda"], TRIFECTA["pb"], TRIFECTA["mcap_assets"]],
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
