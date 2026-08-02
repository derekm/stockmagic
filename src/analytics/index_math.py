"""Index mathematics: S&P DJI divisor method + chained Fisher decomposition.

Implements the design from the StockMonitor suite:
  - Value index   V_t = MV_t / D_t        (S&P Laspeyres-derived, divisor-scaled)
  - Fisher price  P_F = sqrt(L_p * P_p)    (chained OR fixed-base dual-arm)
  - Fisher qty    Q_F = V_t / P_F          (derived; continuity inherited)
  - Nominal       N_t = V_t  (== P_F * Q_F by construction)
  - Return split  ret = ret_price * ret_qty  (geometric)

Two selectable modes (compare them directly):

  mode="chained"  (our innovation)
      To kill the substitution/drift bias we critiqued in the raw cap-weighted
      (Laspeyres-derived) index, the Fisher arms are computed as period-over-
      period *links* against a rolling base window of `chain_n` days; levels are
      the cumulative product of links (base = base_level). Matches the chained
      approach already used in stock_monitor/fisher_index.py.

  mode="fixed"  (S&P-like baseline)
      Single fixed base (Q_0, P_0 from base_date); each Fisher arm carries its
      own divisor so continuity + Fisher symmetry hold exactly as in the S&P
      DJI write-up. This is the apples-to-apples baseline for comparison.

Non-market events (add/delete, corp actions, share/IWF changes) are absorbed by
rescaling the S&P divisor so the value index stays flat across the event. The
Fisher links already exclude price moves, so in chained mode they need no
adjustment; in fixed mode the arms' divisors shift by the same factor k.
"""
from __future__ import annotations

import datetime as dt

import duckdb

from src.data.market_data import MarketDataStore

FIXED_MODE = "fixed"
CHAINED_MODE = "chained"


class IndexMath:
    def __init__(self, store: MarketDataStore, base_date: dt.date,
                 base_level: float = 1000.0, universe: str = "SP500",
                 chain_n: int = 63, mode: str = CHAINED_MODE):
        self.store = store
        self.con = store.conn()
        self.base_date = base_date
        self.base_level = base_level
        self.universe = universe
        self.chain_n = chain_n          # re-anchor base every N trading days
        self.mode = mode                # "chained" (innovation) or "fixed" (baseline)
        self.divisor = None             # set by run_all() from base-date MV

    # ------------------------------------------------------------------ #
    # Value index (S&P divisor) — identical in both modes
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # Fixed-base (S&P-like) Fisher arms
    # ------------------------------------------------------------------ #
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

    def fisher_fixed(self) -> None:
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
                      (a.P_raw/(SELECT P_0 FROM base)*?) ) AS fisher_price_idx,
                a.P_raw/(SELECT P_0 FROM base)*?            AS fisher_price_paasche
            FROM arms a
            """,
            [self.base_date, self.base_level, self.base_level, self.base_level],
        )
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_qty AS
            SELECT a.trade_date,
                ? AS fisher_qty_laspeyres,
                (SELECT SUM(p_0*q_t) FROM idx_panel p JOIN base_snap b
                    ON b.ticker=p.ticker WHERE p.trade_date=a.trade_date)
                / (SELECT SUM(p_0*q_0) FROM base_snap) * ? AS fisher_qty_idx
            FROM (SELECT DISTINCT trade_date FROM idx_panel) a
            """,
            [self.base_level, self.base_level],
        )

    # ------------------------------------------------------------------ #
    # Chained Fisher links + levels
    # ------------------------------------------------------------------ #
    def fisher_chained(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_links AS
            WITH ord AS (
                SELECT trade_date,
                       ROW_NUMBER() OVER (ORDER BY trade_date) - 1 AS rn
                FROM (SELECT DISTINCT trade_date FROM idx_panel)
            ),
            base_day AS (
                SELECT o.trade_date AS t, COALESCE(b.trade_date,
                       (SELECT MIN(trade_date) FROM idx_panel)) AS base_date
                FROM ord o
                LEFT JOIN ord b ON b.rn <= o.rn - ?
                QUALIFY ROW_NUMBER() OVER (PARTITION BY o.trade_date
                                           ORDER BY b.rn DESC NULLS LAST) = 1
            ),
            -- per-ticker series with previous-date values via LAG (no corr subq)
            ser AS (
                SELECT ticker, trade_date, p_t, q_t, mv_t,
                       LAG(p_t) OVER (PARTITION BY ticker ORDER BY trade_date) AS p_prev,
                       LAG(q_t) OVER (PARTITION BY ticker ORDER BY trade_date) AS q_prev
                FROM idx_panel
            ),
            base AS (SELECT trade_date, ticker, p_t AS p_b, q_t AS q_b
                     FROM idx_panel),
            joined AS (
                SELECT s.trade_date, s.ticker, s.p_t, s.q_t, s.mv_t,
                       s.p_prev, s.q_prev, bs.p_b, bs.q_b
                FROM ser s
                JOIN base_day bd ON bd.t = s.trade_date
                JOIN base bs
                  ON bs.trade_date = bd.base_date AND bs.ticker = s.ticker
            )
            SELECT c.trade_date,
                COALESCE(SUM(j.p_t*j.q_b)/NULLIF(SUM(j.p_prev*j.q_b),0),1.0) AS L_p,
                COALESCE(SUM(j.p_t*j.q_t)/NULLIF(SUM(j.p_prev*j.q_t),0),1.0) AS P_p,
                COALESCE(SUM(j.p_b*j.q_t)/NULLIF(SUM(j.p_b*j.q_b),0),1.0) AS L_q,
                COALESCE(SUM(j.p_t*j.q_t)/NULLIF(SUM(j.p_t*j.q_b),0),1.0) AS P_q
            FROM (SELECT DISTINCT trade_date FROM idx_panel) c
            LEFT JOIN joined j ON j.trade_date = c.trade_date
            GROUP BY c.trade_date
            """,
            [self.chain_n],
        )
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_price AS
            SELECT trade_date,
                   ? * EXP(SUM(LN(L_p)) OVER (ORDER BY trade_date)) AS fisher_price_idx,
                   ? * EXP(SUM(LN(P_p)) OVER (ORDER BY trade_date)) AS fisher_price_paasche
            FROM fisher_links
            """,
            [self.base_level, self.base_level],
        )
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_qty AS
            SELECT trade_date,
                   ? * EXP(SUM(LN(L_q)) OVER (ORDER BY trade_date)) AS fisher_qty_laspeyres,
                   ? * EXP(SUM(LN(P_q)) OVER (ORDER BY trade_date)) AS fisher_qty_idx
            FROM fisher_links
            """,
            [self.base_level, self.base_level],
        )

    # ------------------------------------------------------------------ #
    # Nominal decomposition
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    def run_all(self) -> None:
        self.store.build_clean_panel(self.base_date, self.universe)
        mv0 = self.con.execute(
            "SELECT SUM(mv_t) FROM idx_panel WHERE trade_date = ?",
            [self.base_date]).fetchone()[0]
        self.divisor = mv0 / self.base_level
        self.value_index()
        if self.mode == FIXED_MODE:
            self._ensure_base_snapshot()
            self.fisher_fixed()
        else:
            self.fisher_chained()
        self.decompose()

    # ------------------------------------------------------------------ #
    # DIVISOR UPDATE on a non-market event
    # ------------------------------------------------------------------ #
    def apply_event(self, mv_before: float, mv_after: float,
                    event_date: dt.date) -> float:
        """Shift the S&P divisor by factor k so the value index stays flat
        across a non-market event. In fixed mode, also rescale the Fisher arms'
        divisors by k to preserve symmetry. In chained mode the links already
        exclude price moves and need no adjustment. Returns k."""
        k = mv_after / mv_before
        self.divisor = self.divisor * k            # S&P eq. 6 (multiplicative)
        self.value_index()
        if self.mode == FIXED_MODE:
            self.con.execute(
                "UPDATE fisher_arms SET L_den = L_den * ?, P_den = P_den * ?",
                [k, k],
            )
            self.fisher_fixed()
        self.decompose()
        return k
