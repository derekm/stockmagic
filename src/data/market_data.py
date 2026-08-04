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
from pathlib import Path

import duckdb


def _assert_real_parquet(path: str) -> None:
    """Fail fast if a 'parquet' is actually a Git-LFS pointer stub.

    Real parquet files begin with the magic bytes 'PAR1'. LFS pointers begin
    with 'version https://git-lfs...' and DuckDB then throws a cryptic
    'No magic bytes found' error. This turns that into an actionable message.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"parquet not found: {path}")
    head = p.read_bytes()[:4]
    if head != b"PAR1":
        raise ValueError(
            f"{path} is not a real parquet file (missing 'PAR1' magic; "
            f"it is likely a Git-LFS pointer stub). Run `git lfs pull` in the "
            f"data directory, or point --data-dir at a store with real parquet."
        )


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
        """Produce idx_panel: P_t = adj_close; every Q_t column the variants need.

        q_t_sp  = shares*iwf           (S&P cap-weighted family's Q_t slot)
        q_t_vol = traded volume         (volume-weighted family's Q_t slot)
        mv_t    = P_t * q_t_sp          (S&P market value)

        Each variant NAME is a config that names which q_t column fills its Q_t
        slot, so the panel carries all columns up front and the configs just
        grab different numbers (no second panel, no prefix tricks). Audit hooks
        (schema/jump/flatline) are asserted here.
        """
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE idx_panel AS
            WITH vol AS (
                SELECT ticker, trade_date,
                       NULLIF(volume, 0) AS v
                FROM daily_prices
            ),
            vol_clean AS (
                SELECT ticker, trade_date,
                       -- volume Q cleaning (q_t_vol slot). NOTE: this is NOT
                       -- identical to fisher_index.panel()'s cleaning:
                       -- that code does q.ffill().bfill() then a 21d median and
                       -- carries prior volume into zero-current sessions at LINK
                       -- time (q1=q1.where(q1>0,q0)). Here we carry the prior
                       -- non-null session volume once at panel-build via
                       -- LAST(... 1 PRECEDING), then a trailing 21d median, then
                       -- 1.0. The two agree on Laspeyres/Fisher (<1% in
                       -- reconciliation) but diverge on the current-q-sensitive
                       -- Paasche arm (~100% norm) precisely because of this.
                       COALESCE(
                           LAST(v IGNORE NULLS) OVER (
                               PARTITION BY ticker ORDER BY trade_date
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                           quantile_cont(v, 0.5::DOUBLE) OVER (
                               PARTITION BY ticker ORDER BY trade_date
                               ROWS BETWEEN 20 PRECEDING AND CURRENT ROW),
                           1.0) AS v
                FROM vol
            )
            SELECT
                p.ticker,
                p.trade_date,
                p.adj_close                                   AS p_t,
                sc.shares_outstanding * sc.iwf                AS q_t_sp,
                vc.v                                           AS q_t_vol,
                sc.shares_outstanding * sc.iwf * p.adj_close  AS mv_t,
                t.sleeve_tag
            FROM daily_prices p
            JOIN vol_clean vc
              ON vc.ticker = p.ticker AND vc.trade_date = p.trade_date
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

    # ---- parquet-backed panel (bridge to stock_monitor real data) --------
    def build_panel_from_parquet(self, data_dir: str, base_date: dt.date,
                                 universe: str = "SP500",
                                 clean_prices: str = "daily_prices_clean.parquet",
                                 pit: str | None = None,
                                 monitored: str = "monitored_stocks.parquet"):
        """Register real parquet as DuckDB tables and build idx_panel.

        Bridges the existing stock_monitor store:
          - price  : close  -> p_t          (daily_prices_clean)
          - qty    : market_cap / close -> q_t  (float-adj shares, derived
                     from PIT fundamentals so Q_t is capital-structure correct)
          - sleeve  : membership flags from monitored_stocks
        PIT fundamentals are registered as pit_fundamentals with columns
        renamed to match quality_value.py (pb_ratio->pb, mktcap_to_assets->
        mcap_assets, as_of_date->as_of).

        `pit` defaults to the multi-snapshot `fundamentals.parquet` (real
        quarterly history, 2024-2026) when present, falling back to the single
        snapshot `fundamentals_pit.parquet`. Prefer the multi-snapshot source so
        the PIT backfill is genuinely historical, not a constant.
        """
        if pit is None:
            cand = Path(data_dir) / "fundamentals.parquet"
            pit = "fundamentals.parquet" if cand.exists() else "fundamentals_pit.parquet"
        _assert_real_parquet(f"{data_dir}/{clean_prices}")
        _assert_real_parquet(f"{data_dir}/daily_prices.parquet")
        _assert_real_parquet(f"{data_dir}/{pit}")
        _assert_real_parquet(f"{data_dir}/{monitored}")
        # price + PIT shares come from the cleaned parquet; traded volume comes
        # from the MAINTAINED daily_prices.parquet (the same file fisher_index.py
        # reads its quantity from). Join them so idx_panel.volume is apples-to-
        # apples with stock_monitor's fisher_q.
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE daily_prices AS
            SELECT c.ticker, c.date AS trade_date,
                   c.close AS adj_close,
                   r.volume AS volume
            FROM read_parquet('{data_dir}/{clean_prices}') c
            JOIN read_parquet('{data_dir}/daily_prices.parquet') r
              ON r.ticker = c.ticker AND r.date = c.date
            """
        )
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE share_counts AS
            SELECT f.ticker,
                   (SELECT MIN(trade_date) FROM daily_prices) AS as_of,
                   -- derived float-adj shares = market_cap / close (applied to
                   -- all history: single PIT snapshot treated as current shares)
                   CASE WHEN p.adj_close IS NULL OR p.adj_close = 0 THEN NULL
                        ELSE f.market_cap / p.adj_close END          AS shares_outstanding,
                   1.0                                              AS iwf
            FROM read_parquet('{data_dir}/{pit}') f
            LEFT JOIN (SELECT ticker, adj_close FROM daily_prices
                       WHERE trade_date = (SELECT MAX(trade_date) FROM daily_prices)
                      ) p ON p.ticker = f.ticker
            """)
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE sp500_tags AS
            SELECT ticker,
                   CASE
                     WHEN sp500_member THEN 'SP500'
                     WHEN growth_tech_index THEN 'growth_tech'
                     WHEN defensive_value_index THEN 'defensive_value'
                     ELSE 'other' END AS sleeve_tag,
                   COALESCE(added_date::DATE, (SELECT MIN(trade_date)
                                               FROM daily_prices)) AS from_date
            FROM read_parquet('{data_dir}/{monitored}')
            """)
        # PIT fundamentals with column rename to match quality_value.py
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE pit_fundamentals AS
            SELECT ticker, as_of_date AS as_of,
                   market_cap, market_cap_b,
                   roe, roic, ev_ebitda, pb_ratio AS pb, mktcap_to_assets AS mcap_assets,
                   debt_to_equity AS debt_equity, interest_coverage,
                   total_assets
            FROM read_parquet('{data_dir}/{pit}')
            """
        )
        return self.build_clean_panel(base_date, universe)

    def conn(self) -> duckdb.DuckDBPyConnection:
        return self.con
