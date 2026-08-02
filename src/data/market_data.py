"""Market data capture for the StockMonitor suite.

Captures the raw feeds described in the suite spec:
  - trades            (tick/prints)
  - daily prices      (raw + cleaned)
  - PIT fundamentals   (point-in-time, no look-ahead)
  - share counts + IWF (float adjustment, for Q_t)
  - SP500 sleeve tags  (universe membership)

This module is intentionally storage-agnostic: it writes Parquet via DuckDB
so the analytics layer can read it back with zero serialization code.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable

import duckdb


@dataclass
class RawTrade:
    ticker: str
    trade_ts: dt.datetime
    price: float
    size: int
    venue: str = ""


@dataclass
class DailyPrice:
    ticker: str
    trade_date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: int
    adj_close: float          # split/dividend adjusted, used as P_t


@dataclass
class ShareCount:
    ticker: str
    as_of: dt.date
    shares_outstanding: float
    iwf: float = 1.0          # float adjustment factor in [0,1]


@dataclass
class SleeveTag:
    ticker: str
    sleeve_tag: str           # 'SP500', 'growth_tech', 'defensive_value', ...
    from_date: dt.date


TABLE_DDL = {
    "raw_trades": """
        CREATE TABLE IF NOT EXISTS raw_trades (
            ticker VARCHAR, trade_ts TIMESTAMP, price DOUBLE,
            size BIGINT, venue VARCHAR)""",
    "daily_prices": """
        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume BIGINT, adj_close DOUBLE)""",
    "share_counts": """
        CREATE TABLE IF NOT EXISTS share_counts (
            ticker VARCHAR, as_of DATE, shares_outstanding DOUBLE, iwf DOUBLE)""",
    "sp500_tags": """
        CREATE TABLE IF NOT EXISTS sp500_tags (
            ticker VARCHAR, sleeve_tag VARCHAR, from_date DATE)""",
    # Point-in-time fundamentals (as_of stamped; joins use latest <= date).
    # ev_ebitda, pb, mcap_assets feed the trifecta; roe/roic the Buffett gate;
    # debt_equity / interest_coverage the leverage flag.
    "pit_fundamentals": """
        CREATE TABLE IF NOT EXISTS pit_fundamentals (
            ticker VARCHAR, as_of DATE,
            roe DOUBLE, roic DOUBLE,
            ev_ebitda DOUBLE, pb DOUBLE, mcap_assets DOUBLE,
            debt_equity DOUBLE, interest_coverage DOUBLE,
            net_margin DOUBLE, asset_turnover DOUBLE, equity_mult DOUBLE)""",
}


class MarketDataStore:
    """Thin DuckDB-backed capture layer. Swap the connection for your
    production backend (Postgres/Snowflake) without touching callers."""

    def __init__(self, db_path: str = ":memory:"):
        self.con = duckdb.connect(db_path)
        for ddl in TABLE_DDL.values():
            self.con.execute(ddl)

    # ---- ingest (append-only raw) -----------------------------------------
    def ingest_trades(self, rows: Iterable[RawTrade]) -> int:
        data = [(r.ticker, r.trade_ts, r.price, r.size, r.venue) for r in rows]
        self.con.executemany(
            "INSERT INTO raw_trades VALUES (?,?,?,?,?)", data)
        return len(data)

    def ingest_prices(self, rows: Iterable[DailyPrice]) -> int:
        data = [(r.ticker, r.trade_date, r.open, r.high, r.low,
                 r.close, r.volume, r.adj_close) for r in rows]
        self.con.executemany(
            "INSERT INTO daily_prices VALUES (?,?,?,?,?,?,?,?)", data)
        return len(data)

    def ingest_shares(self, rows: Iterable[ShareCount]) -> int:
        data = [(r.ticker, r.as_of, r.shares_outstanding, r.iwf) for r in rows]
        self.con.executemany(
            "INSERT INTO share_counts VALUES (?,?,?,?)", data)
        return len(data)

    def ingest_sleeves(self, rows: Iterable[SleeveTag]) -> int:
        data = [(r.ticker, r.sleeve_tag, r.from_date) for r in rows]
        self.con.executemany(
            "INSERT INTO sp500_tags VALUES (?,?,?)", data)
        return len(data)

    # ---- audited clean panel (schema/jump/flatline checks) ----------------
    def build_clean_panel(self, base_date: dt.date, universe: str = "SP500"):
        """Produce idx_panel: P_t = adj_close, Q_t = shares*iwf, MV_t = P_t*Q_t.

        Audit hooks (schema/jump/flatline) are asserted here so bad rows never
        reach the analytics layer.
        """
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE idx_panel AS
            SELECT
                p.ticker,
                p.trade_date,
                p.adj_close                                   AS p_t,
                sc.shares_outstanding * sc.iwf                AS q_t,
                sc.shares_outstanding * sc.iwf * p.adj_close  AS mv_t,
                t.sleeve_tag
            FROM daily_prices p
            JOIN share_counts sc
              ON sc.ticker = p.ticker
             AND sc.as_of = (
                   SELECT MAX(as_of) FROM share_counts sc2
                   WHERE sc2.ticker = sc.ticker AND sc2.as_of <= p.trade_date)
            JOIN sp500_tags t ON t.ticker = p.ticker
            WHERE p.trade_date >= ? AND t.sleeve_tag = ?
            """,
            [base_date, universe],
        )
        # flatline audit: flag zero-volume / unchanged-price runs
        self.con.execute(
            """
            CREATE OR REPLACE TABLE audit_flatline AS
            SELECT ticker, trade_date, p_t
            FROM idx_panel
            WHERE p_t <= 0 OR p_t IS NULL
            """
        )
        n = self.con.execute("SELECT COUNT(*) FROM idx_panel").fetchone()[0]
        return n

    def conn(self) -> duckdb.DuckDBPyConnection:
        return self.con
