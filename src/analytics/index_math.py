"""Index mathematics — parallel multi-variant divisor framework.

We maintain SIX index constructions side by side, every one on the same
`idx_panel` basket, so they can be compared, combined, and stress-tested
without rebuilding the pipeline:

  S&P divisor (value)        V_t = SUM(P_t Q_t) / D_t
  Laspeyres price            L_p = SUM(P_t Q_0) / SUM(P_0 Q_0)
  Paasche  price             P_p = SUM(P_t Q_t) / SUM(P_0 Q_t)
  Fisher   price             F   = sqrt(L_p * P_p)              (ideal)
  chained Fisher (ours)      F*  = chain of period links w/ rolling base
  chained Laspeyres (ours)   L*  = chain of Laspeyres links w/ rolling base

Divisor families
----------------
* S&P single divisor D  — one number, re-scaled only on non-market events
  (constituent add/delete, corp action). Continuity by construction.
* Fixed arms divisors  — each fixed-base arm (L, P, F) carries its own divisor
  so Fisher symmetry and the identity F = sqrt(L*P) hold exactly.
* OUR parallel divisor  — the *chained* arms use a rolling base and chain
  period links, so there is no fixed Q_0 to drift. This is the methodology
  improvement we own: a divisor that re-anchors continuously instead of
  accumulating substitution bias between discrete events.

Every variant's divisor(s) live in the `divisors` table so `apply_event`
can re-scale all of them atomically (S&P eq. 6 for the S&P divisor; factor k
on the fixed arms' divisors; multiplicative scale on the chained levels'
links at the event boundary).

Identities that must hold (asserted in tests):
  F  == sqrt(L_p * P_p)              (Fisher symmetry, fixed)
  V_t == F_t * Q_t                  (value == price * quantity; Q_t = V_t/F_t)
  chained levels start at base_level and chain links multiply correctly.
"""
from __future__ import annotations

import datetime as dt

import duckdb

from src.data.market_data import MarketDataStore

# variant keys
SNP = "sp_value"
LASPEYRES = "laspeyres"
PAASCHE = "paasche"
FISHER = "fisher"
CHAINED_FISHER = "chained_fisher"
CHAINED_LASPEYRES = "chained_laspeyres"

FIXED_BASED = {SNP, LASPEYRES, PAASCHE, FISHER}
CHAINED_BASED = {CHAINED_FISHER, CHAINED_LASPEYRES}

ALL_VARIANTS = (SNP, LASPEYRES, PAASCHE, FISHER, CHAINED_FISHER, CHAINED_LASPEYRES)


class IndexMath:
    def __init__(self, store: MarketDataStore, base_date: dt.date,
                 base_level: float = 1000.0, universe: str = "SP500",
                 chain_n: int = 63):
        self.store = store
        self.con = store.conn()
        self.base_date = base_date
        self.base_level = base_level
        self.universe = universe
        self.chain_n = chain_n          # re-anchor base every N trading days
        self.divisors: dict = {}        # variant -> divisor (per-arm for fixed)
        self.divisor = None             # S&P single divisor (convenience)

    # ------------------------------------------------------------------ #
    # Base snapshot (Q_0, P_0 from base_date) — for fixed-base variants
    # ------------------------------------------------------------------ #
    def _ensure_base_snapshot(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TEMP TABLE base_snap AS
            SELECT p.ticker, p.p_t AS p_0, p.q_t AS q_0
            FROM idx_panel p
            WHERE p.trade_date = ?
            """,
            [self.base_date],
        )

    # ------------------------------------------------------------------ #
    # S&P value index + single divisor
    # ------------------------------------------------------------------ #
    def value_index(self) -> None:
        mv0 = self.con.execute(
            "SELECT SUM(mv_t) FROM idx_panel WHERE trade_date = ?",
            [self.base_date]).fetchone()[0]
        self.divisor = mv0 / self.base_level
        self.divisors[SNP] = self.divisor
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
    # Fixed-base arms (Laspeyres, Paasche, Fisher) — each its own divisor
    # ------------------------------------------------------------------ #
    def fisher_fixed(self) -> None:
        l_den = self.con.execute("SELECT SUM(p_0*q_0) FROM base_snap").fetchone()[0]
        self.divisors[LASPEYRES] = l_den / self.base_level
        self.divisors[PAASCHE] = l_den / self.base_level     # rescaled at end
        self.divisors[FISHER] = l_den / self.base_level
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_arms AS
            SELECT
                p.trade_date,
                SUM(p.p_t * b.q_0)            AS L_num,
                ?                             AS L_den,
                SUM(p.mv_t)                   AS P_num,
                SUM(b.p_0 * p.q_t)            AS P_den
            FROM idx_panel p
            JOIN base_snap b ON b.ticker = p.ticker
            GROUP BY p.trade_date
            """,
            [l_den],
        )
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_fixed_levels AS
            WITH arms AS (
                SELECT trade_date, L_num/L_den AS L_raw, P_num/P_den AS P_raw
                FROM fisher_arms
            ),
            base AS (
                SELECT L_raw AS L_0, P_raw AS P_0
                FROM arms WHERE trade_date = ?
            )
            SELECT a.trade_date,
                a.L_raw / (SELECT L_0 FROM base) * ?             AS laspeyres_idx,
                a.P_raw / (SELECT P_0 FROM base) * ?             AS paasche_idx,
                SQRT((a.L_raw/(SELECT L_0 FROM base)) *
                     (a.P_raw/(SELECT P_0 FROM base))) * ?       AS fisher_idx
            FROM arms a
            """,
            [self.base_date, self.base_level, self.base_level, self.base_level],
        )

    # ------------------------------------------------------------------ #
    # Chained arms (Fisher + Laspeyres) — OUR parallel divisor methodology
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
                COALESCE(SUM(j.p_b*j.q_t)/NULLIF(SUM(j.p_b*j.q_b),0),1.0) AS L_q
            FROM (SELECT DISTINCT trade_date FROM idx_panel) c
            LEFT JOIN joined j ON j.trade_date = c.trade_date
            GROUP BY c.trade_date
            """,
            [self.chain_n],
        )
        self.con.execute(
            """
            CREATE OR REPLACE TABLE fisher_chained_levels AS
            SELECT trade_date,
                ? * EXP(SUM(LN(L_p)) OVER (ORDER BY trade_date)) AS chained_laspeyres_idx,
                ? * EXP(SUM(LN(SQRT(L_p*P_p))) OVER (ORDER BY trade_date)) AS chained_fisher_idx
            FROM fisher_links
            """,
            [self.base_level, self.base_level],
        )
        # rolling-base divisor bookkeeping: store the effective base date per step
        self.con.execute(
            """
            CREATE OR REPLACE TABLE chain_base_map AS
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
            )
            SELECT t AS trade_date, base_date FROM base_day
            """,
            [self.chain_n],
        )

    # ------------------------------------------------------------------ #
    # Unify all variants into one long table keyed by `variant`
    # ------------------------------------------------------------------ #
    def unify(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TABLE index_levels AS
            SELECT trade_date, 'sp_value'        AS variant, value_idx AS idx FROM value_index
            UNION ALL
            SELECT trade_date, 'laspeyres'       AS variant, laspeyres_idx     FROM fisher_fixed_levels
            UNION ALL
            SELECT trade_date, 'paasche'         AS variant, paasche_idx       FROM fisher_fixed_levels
            UNION ALL
            SELECT trade_date, 'fisher'          AS variant, fisher_idx        FROM fisher_fixed_levels
            UNION ALL
            SELECT trade_date, 'chained_laspeyres'AS variant, chained_laspeyres_idx FROM fisher_chained_levels
            UNION ALL
            SELECT trade_date, 'chained_fisher'  AS variant, chained_fisher_idx    FROM fisher_chained_levels
            """
        )
        # derived quantity arms: Q_t = value / price (for each price variant)
        self.con.execute(
            """
            CREATE OR REPLACE TABLE qty_levels AS
            SELECT v.trade_date, v.variant,
                   NULLIF(v.idx, 0) AS price_idx,
                   (SELECT value_idx FROM value_index vi
                    WHERE vi.trade_date = v.trade_date) / NULLIF(v.idx, 0) AS qty_idx
            FROM index_levels v
            WHERE v.variant IN ('laspeyres','paasche','fisher',
                                'chained_laspeyres','chained_fisher')
            """
        )
        # divisor registry (one row per maintained divisor)
        self.con.execute(
            "CREATE OR REPLACE TABLE divisors AS "
            "SELECT * FROM (VALUES "
            "('sp_value', ?), ('laspeyres', ?), ('paasche', ?), ('fisher', ?)) "
            "AS t(variant, divisor)",
            [self.divisors.get(SNP), self.divisors.get(LASPEYRES),
             self.divisors.get(PAASCHE), self.divisors.get(FISHER)],
        )
        # chained divisor is implicit (rolling base) — record metadata
        self.con.execute(
            "INSERT INTO divisors VALUES ('chained_fisher', NULL), "
            "('chained_laspeyres', NULL)"
        )

    # ------------------------------------------------------------------ #
    # Nominal decomposition for every price variant (ret split price/qty)
    # ------------------------------------------------------------------ #
    def decompose(self) -> None:
        self.con.execute(
            """
            CREATE OR REPLACE TABLE nominal_decomp AS
            WITH v AS (SELECT trade_date, value_idx AS nominal_idx FROM value_index),
            p AS (SELECT trade_date, variant, idx AS price_idx FROM index_levels
                  WHERE variant IN ('laspeyres','paasche','fisher',
                                    'chained_laspeyres','chained_fisher'))
            SELECT
                p.trade_date, p.variant,
                v.nominal_idx,
                p.price_idx,
                v.nominal_idx / NULLIF(p.price_idx,0) AS qty_idx,
                v.nominal_idx / LAG(v.nominal_idx) OVER w - 1            AS ret_total,
                p.price_idx  / LAG(p.price_idx)  OVER w - 1             AS ret_price,
                (v.nominal_idx/NULLIF(p.price_idx,0)) /
                    LAG(v.nominal_idx/NULLIF(p.price_idx,0)) OVER w - 1  AS ret_qty
            FROM v JOIN p ON p.trade_date = v.trade_date
            WINDOW w AS (PARTITION BY p.variant ORDER BY v.trade_date)
            """
        )

    # ------------------------------------------------------------------ #
    def run_all(self) -> None:
        self.store.build_clean_panel(self.base_date, self.universe)
        self._ensure_base_snapshot()
        self.value_index()          # S&P divisor
        self.fisher_fixed()         # Laspeyres / Paasche / Fisher arms
        self.fisher_chained()       # OUR chained arms
        self.unify()
        self.decompose()

    # ------------------------------------------------------------------ #
    # DIVISOR UPDATE on a non-market event — all variants at once
    # ------------------------------------------------------------------ #
    def apply_event(self, mv_before: float, mv_after: float,
                    event_date: dt.date) -> float:
        """Re-scale every maintained divisor so the index family stays flat
        across a non-market event (S&P eq. 6). Returns the scale factor k.

          * S&P single divisor D  -> D * k   (value index unchanged)
          * fixed arms divisors    -> * k    (Laspeyres/Paasche/Fisher symmetry kept)
          * chained arms           -> the event splits the link chain; we rescale
                                      all post-event chained levels by k so the
                                      reported level is continuous (the rolling base
                                      already makes the *path* event-invariant).
        """
        k = mv_after / mv_before
        # S&P + fixed arms
        self.divisor = self.divisor * k
        self.divisors[SNP] = self.divisor
        for v in (LASPEYRES, PAASCHE, FISHER):
            self.divisors[v] = self.divisors[v] * k
        self.con.execute("UPDATE fisher_arms SET L_den = L_den * ?, P_den = P_den * ?",
                         [k, k])
        self.value_index()          # recompute with new divisor
        self.fisher_fixed()         # recompute arms with new divisors
        # chained arms: rescale post-event levels by k (path continuity)
        self.con.execute(
            """
            UPDATE fisher_chained_levels
            SET chained_fisher_idx   = chained_fisher_idx * ?,
                chained_laspeyres_idx = chained_laspeyres_idx * ?
            WHERE trade_date > ?
            """,
            [k, k, event_date],
        )
        self.unify()
        self.decompose()
        return k
