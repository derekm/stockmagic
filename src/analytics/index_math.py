"""Index mathematics: S&P DJI divisor method + chained Fisher decomposition.

Implements the design from the StockMonitor suite:
  - Value index   V_t = MV_t / D_t        (S&P Laspeyres-derived, divisor-scaled)
  - Fisher price  P_F = sqrt(L_p * P_p)    (dual-arm divisor: D_L, D_P)
  - Fisher qty    Q_F = V_t / P_F          (derived; continuity inherited)
  - Nominal       N_t = V_t  (== P_F * Q_F by construction)
  - Return split  ret = ret_price * ret_qty  (geometric)

All divisor moves happen ONLY on non-market events (add/delete, corp actions,
share-count or IWF changes). Between events the level drifts with price.
"""
from __future__ import annotations

import datetime as dt
import math

import duckdb

from src.data.market_data import MarketDataStore


class IndexMath:
    def __init__(self, store: MarketDataStore, base_date: dt.date,
                 base_level: float = 1000.0, universe: str = "SP500"):
        self.store = store
        self.con = store.conn()
        self.base_date = base_date
        self.base_level = base_level
        self.universe = universe
        self.divisor = None  # set by run_all() from base-date MV

    # -- base snapshot (Q_0, P_0) for the Fisher arms -----------------------
    def _ensure_base_snapshot(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TEMP TABLE base_snap AS
            SELECT p.ticker, p.p_t AS p_0, p.q_t AS q_0, p.mv_t AS mv_0
            FROM idx_panel p
            WHERE p.trade_date = ?
            """,
            [self.base_date],
        )

    # -- value index (S&P divisor) -----------------------------------------
    def value_index(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TABLE value_index AS
            SELECT trade_date, mv_t / ? AS value_idx
            FROM (SELECT trade_date, SUM(mv_t) AS mv_t FROM idx_panel
                  GROUP BY trade_date)
            """,
            [self.divisor],
        )

    # -- Fisher price (dual-arm divisor) -----------------------------------
    def fisher_price(self) -> None:
        l_den = self.con.execute(
            "SELECT SUM(p_0 * q_0) FROM base_snap").fetchone()[0]
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_arms AS
            SELECT
                p.trade_date,
                SUM(p.p_t * b.q_0)                              AS L_num,
                ?                                              AS L_den,
                SUM(p.mv_t)                                    AS P_num,
                SUM(b.p_0 * p.q_t)                             AS P_den
            FROM idx_panel p
            JOIN base_snap b ON b.ticker = p.ticker
            GROUP BY p.trade_date
            """,
            [l_den],
        )
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_price AS
            WITH arms AS (
                SELECT trade_date, L_num/L_den AS L_raw, P_num/P_den AS P_raw
                FROM fisher_arms
            ),
            base AS (
                SELECT L_raw AS L_0, P_raw AS P_0
                FROM arms WHERE trade_date = ?
            )
            SELECT a.trade_date,
                SQRT( (a.L_raw/(SELECT L_0 FROM base)*?) *
                      (a.P_raw/(SELECT P_0 FROM base)*?) ) AS fisher_price_idx
            FROM arms a
            """,
            [self.base_date, self.base_level, self.base_level],
        )

    # -- Fisher qty (derived) + nominal decomposition ----------------------
    def decompose(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TABLE nominal_decomp AS
            SELECT
                v.trade_date,
                v.value_idx                                           AS nominal_idx,
                f.fisher_price_idx                                    AS price_idx,
                v.value_idx / f.fisher_price_idx                      AS qty_idx,
                v.value_idx / LAG(v.value_idx) OVER w - 1             AS ret_total,
                f.fisher_price_idx / LAG(f.fisher_price_idx) OVER w - 1 AS ret_price,
                (v.value_idx / f.fisher_price_idx) /
                    LAG(v.value_idx / f.fisher_price_idx) OVER w - 1  AS ret_qty
            FROM value_index v
            JOIN fisher_price f ON f.trade_date = v.trade_date
            WINDOW w AS (ORDER BY v.trade_date)
            """
        )

    def run_all(self) -> None:
        self.store.build_clean_panel(self.base_date, self.universe)
        self._ensure_base_snapshot()
        # initial divisor so that base-date level == base_level
        mv0 = self.con.execute(
            "SELECT SUM(mv_t) FROM idx_panel WHERE trade_date = ?",
            [self.base_date]).fetchone()[0]
        self.divisor = mv0 / self.base_level
        self.value_index()
        self.fisher_price()
        self.decompose()

    # -- DIVISOR UPDATE on a non-market event -------------------------------
    def apply_event(self, mv_before: float, mv_after: float,
                    event_date: dt.date) -> float:
        """Shift the divisor by the same factor k so continuity + Fisher
        symmetry hold. A non-market event changes MV by k at constant prices;
        to keep the level flat we scale the divisor by k, which rescales the
        whole series by 1/k. Returns the event factor k."""
        k = mv_after / mv_before
        self.divisor = self.divisor * k            # S&P eq. 6 (multiplicative)
        self.value_index()                          # re-run with new divisor
        # Fisher arms: shift both divisors by k (symmetric)
        self.con.execute(
            "UPDATE fisher_arms SET L_den = L_den * ?, P_den = P_den * ?",
            [k, k],
        )
        self.fisher_price()
        self.decompose()
        return k
