"""Index mathematics — parallel multi-variant divisor framework.

We maintain SEVEN index constructions side by side, every one on the same
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

S&P 500 technique vs OUR rolling rebase (important distinction)
---------------------------------------------------------------
These are DIFFERENT constructions. The 63-trading-day rolling base is
stockmagic's own chained-index choice — it is NOT S&P methodology.

S&P 500 (per SPDJI Index Mathematics methodology + S&P 500 factsheet):
  * Float-adjusted market-cap weighted:  level = SUM(P_t * shares * IWF) / D_t
    where IWF = investable float / shares outstanding.
  * FIXED base period: the 1941–43 aggregate market value is set to an indexed
    level of 10 (the "1941–43 = 10" convention). This base constant NEVER
    moves.
  * The DIVISOR D_t is the only continuity mechanism. It is re-scaled ONLY on
    non-market events (constituent add/delete, corporate actions) via the
    standard divisor-adjustment equation, so the index level stays continuous
    across the event. There is no periodic rebase, no rolling window, and no
    chaining of period links in the S&P 500 level itself.

OUR chained arms (vol_chained_*, sp_chained_*, trad_chained_*):
  * Re-anchor the base every `chain_n` = 63 trading days (~3 months) and chain
    the adjacent period links (t-1 -> t) in between. This is the classic
    chained-index de-biasing idea (the same family as the U.S. "chained CPI",
    C-CPI-U): a fixed-base Laspeyres/Paasche drifts from the ideal chain
    because of substitution bias, and re-anchoring periodically reduces it.
  * 63 trading days is stockmagic's PARAMETER choice, not a standard. S&P does
    not chain its index this way at all.

Reconciliation implication: comparing stockmagic's chained arms to a
fixed-base or t-1->t chain (e.g. stock_monitor's fisher_indexes) leaves a
residual that is EXACTLY this base-window difference. In practice the
Fisher/Laspeyres pairs reconcile to <1% norm (the documented de-biasing),
which is the expected, correct gap — not a bug. The Paasche arm's larger gap
is a separate volume-cleaning difference (see market_data.build_clean_panel).

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

# =============================================================================
# VARIANT CONFIGS — each variant NAME is a config that grabs a different number
# from the clean panel. The panel carries every Q_t column up front
# (q_t_sp = shares*iwf, q_t_vol = cleaned traded volume); the config just names
# which column fills its Q_t slot. No if/else on names, no dynamic prefixes.
#
# construction: one of the seven index constructions (value / laspeyres / paasche
#               / fisher / chained_laspeyres / chained_paasche / chained_fisher)
# q_col:       panel column that supplies this variant's Q_t
# method:      which divisor method (stockmagic chained, or traditional fixed-base)
# =============================================================================
_VARIANT_CONFIGS = [
    # --- S&P cap-weighted family (q_t_sp = shares*iwf) ---
    dict(name="sp_value",            q_col="q_t_sp", construction="value",            method="sp_divisor"),
    dict(name="sp_laspeyres",        q_col="q_t_sp", construction="laspeyres",        method="fixed"),
    dict(name="sp_paasche",          q_col="q_t_sp", construction="paasche",          method="fixed"),
    dict(name="sp_fisher",           q_col="q_t_sp", construction="fisher",          method="fixed"),
    dict(name="sp_chained_laspeyres",q_col="q_t_sp", construction="chained_laspeyres",method="chained"),
    dict(name="sp_chained_fisher",   q_col="q_t_sp", construction="chained_fisher",  method="chained"),
    # --- volume-weighted family (q_t_vol = cleaned traded volume) ---
    dict(name="vol_value",           q_col="q_t_vol", construction="value",            method="sp_divisor"),
    dict(name="vol_laspeyres",       q_col="q_t_vol", construction="laspeyres",        method="fixed"),
    dict(name="vol_paasche",         q_col="q_t_vol", construction="paasche",          method="fixed"),
    dict(name="vol_fisher",          q_col="q_t_vol", construction="fisher",          method="fixed"),
    dict(name="vol_chained_laspeyres",q_col="q_t_vol",construction="chained_laspeyres",method="chained"),
    dict(name="vol_chained_paasche", q_col="q_t_vol", construction="chained_paasche",  method="chained"),
    dict(name="vol_chained_fisher",  q_col="q_t_vol", construction="chained_fisher",  method="chained"),
    # --- traditional Fisher fixed-base family (q_t_sp, classic sqrt(L*P)) ---
    dict(name="trad_laspeyres",      q_col="q_t_sp", construction="laspeyres",        method="traditional"),
    dict(name="trad_paasche",        q_col="q_t_sp", construction="paasche",          method="traditional"),
    dict(name="trad_fisher",         q_col="q_t_sp", construction="fisher",          method="traditional"),
    dict(name="trad_value",          q_col="q_t_sp", construction="value",            method="traditional"),
    dict(name="trad_chained_laspeyres",q_col="q_t_sp",construction="chained_laspeyres",method="traditional"),
    dict(name="trad_chained_fisher", q_col="q_t_sp", construction="chained_fisher",  method="traditional"),
]

# quick lookups
_VARIANTS_BY_NAME = {c["name"]: c for c in _VARIANT_CONFIGS}
ALL_VARIANTS = tuple(c["name"] for c in _VARIANT_CONFIGS)


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
    # The compute is Q-agnostic given a q column. We build the arms once per
    # distinct q_col (q_t_sp, q_t_vol); each variant config then grabs the
    # construction+method it names from the matching intermediate tables.
    # ------------------------------------------------------------------ #
    def _q_cols(self) -> list:
        seen, out = set(), []
        for c in _VARIANT_CONFIGS:
            if c["q_col"] not in seen:
                seen.add(c["q_col"]); out.append(c["q_col"])
        return out

    def _ensure_base_snapshot(self, q_col: str) -> None:
        self.con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE base_snap_{q_col} AS
            SELECT p.ticker, p.p_t AS p_0, p.{q_col} AS q_0
            FROM idx_panel p
            WHERE p.trade_date = ?
            """,
            [self.base_date],
        )

    # ------------------------------------------------------------------ #
    # S&P value index + single divisor (per q_col)
    # ------------------------------------------------------------------ #
    def value_index(self, q_col: str) -> None:
        mv0 = self.con.execute(
            f"SELECT SUM(p_t * {q_col}) FROM idx_panel WHERE trade_date = ?",
            [self.base_date]).fetchone()[0]
        self.divisors[f"sp_value::{q_col}"] = mv0 / self.base_level
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE value_index_{q_col} AS
            SELECT trade_date, (SUM(p_t * {q_col}) / ?) AS value_idx
            FROM idx_panel GROUP BY trade_date
            """,
            [self.base_level],
        )

    # ------------------------------------------------------------------ #
    # Fixed-base arms (Laspeyres, Paasche, Fisher) — each its own divisor
    # ------------------------------------------------------------------ #
    def fisher_fixed(self, q_col: str) -> None:
        l_den = self.con.execute(
            f"SELECT SUM(p_0*q_0) FROM base_snap_{q_col}").fetchone()[0]
        self.divisors[f"laspeyres::{q_col}"] = l_den / self.base_level
        self.divisors[f"paasche::{q_col}"]   = l_den / self.base_level
        self.divisors[f"fisher::{q_col}"]    = l_den / self.base_level
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE fisher_arms_{q_col} AS
            SELECT
                p.trade_date,
                SUM(p.p_t * b.q_0)            AS L_num,
                ?                             AS L_den,
                SUM(p.p_t * p.{q_col})        AS P_num,
                SUM(b.p_0 * p.{q_col})        AS P_den
            FROM idx_panel p
            JOIN base_snap_{q_col} b ON b.ticker = p.ticker
            GROUP BY p.trade_date
            """,
            [l_den],
        )
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE fisher_fixed_levels_{q_col} AS
            WITH arms AS (
                SELECT trade_date, L_num/L_den AS L_raw, P_num/P_den AS P_raw
                FROM fisher_arms_{q_col}
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
    # Chained arms (Fisher + Laspeyres) — rolling base
    # ------------------------------------------------------------------ #
    def fisher_chained(self, q_col: str) -> None:
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE fisher_links_{q_col} AS
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
                SELECT ticker, trade_date, p_t, {q_col} AS q_t,
                       LAG(p_t) OVER (PARTITION BY ticker ORDER BY trade_date) AS p_prev,
                       LAG({q_col}) OVER (PARTITION BY ticker ORDER BY trade_date) AS q_prev
                FROM idx_panel
            ),
            base AS (SELECT trade_date, ticker, p_t AS p_b, {q_col} AS q_b
                     FROM idx_panel),
            joined AS (
                SELECT s.trade_date, s.ticker, s.p_t, s.q_t,
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
            f"""
            CREATE OR REPLACE TABLE fisher_chained_levels_{q_col} AS
            SELECT trade_date,
                ? * EXP(SUM(LN(L_p)) OVER (ORDER BY trade_date)) AS chained_laspeyres_idx,
                ? * EXP(SUM(LN(P_p)) OVER (ORDER BY trade_date)) AS chained_paasche_idx,
                ? * EXP(SUM(LN(SQRT(L_p*P_p))) OVER (ORDER BY trade_date)) AS chained_fisher_idx
            FROM fisher_links_{q_col}
            """,
            [self.base_level, self.base_level, self.base_level],
        )

    # ------------------------------------------------------------------ #
    # Unify all 18 variant configs into one long table keyed by `variant`
    # ------------------------------------------------------------------ #
    def _src_for(self, cfg: dict) -> tuple[str, str]:
        """(source_table, value_column) for a variant config."""
        q = cfg["q_col"]; c = cfg["construction"]
        if c == "value":
            return f"value_index_{q}", "value_idx"
        if c in ("laspeyres", "paasche", "fisher"):
            return f"fisher_fixed_levels_{q}", f"{c}_idx"
        if c == "chained_laspeyres":
            return f"fisher_chained_levels_{q}", "chained_laspeyres_idx"
        if c == "chained_paasche":
            return f"fisher_chained_levels_{q}", "chained_paasche_idx"
        if c == "chained_fisher":
            return f"fisher_chained_levels_{q}", "chained_fisher_idx"
        raise ValueError(c)

    def _qty_and_divisors(self) -> None:
        # derived quantity arms: Q_t = value / price (for each price variant).
        # Each variant's value level comes from value_index_<its q_col>.
        self.con.execute(
            "CREATE OR REPLACE TABLE qty_levels AS "
            "SELECT NULL::DATE AS trade_date, NULL::VARCHAR AS variant, "
            "NULL::DOUBLE AS price_idx, NULL::DOUBLE AS qty_idx WHERE FALSE")
        for cfg in _VARIANT_CONFIGS:
            if cfg["construction"] == "value":
                continue
            self.con.execute(
                f"""
                INSERT INTO qty_levels
                SELECT v.trade_date, v.variant,
                       NULLIF(v.idx, 0) AS price_idx,
                       (SELECT vi.value_idx FROM value_index_{cfg['q_col']} vi
                        WHERE vi.trade_date = v.trade_date) / NULLIF(v.idx, 0) AS qty_idx
                FROM index_levels v
                WHERE v.variant = '{cfg['name']}'
                """
            )
        # divisor registry (one row per maintained divisor)
        self.con.execute(
            "CREATE OR REPLACE TABLE divisors AS "
            "SELECT * FROM (VALUES " +
            ", ".join(f"('{n}', ?)" for n in ALL_VARIANTS) +
            ") AS t(variant, divisor)",
            [self.divisors.get(f"{n}", None) for n in ALL_VARIANTS],
        )

    # ------------------------------------------------------------------ #
    # Nominal decomposition for every price variant (ret split price/qty)
    # ------------------------------------------------------------------ #
    def decompose(self) -> None:
        # build per-variant price table + a value table per q_col
        val_union = " UNION ALL ".join(
            f"SELECT trade_date, '{cfg['name']}' AS variant, value_idx AS nominal_idx "
            f"FROM value_index_{cfg['q_col']}" for cfg in _VARIANT_CONFIGS)
        price_union = " UNION ALL ".join(
            f"SELECT trade_date, '{cfg['name']}' AS variant, idx AS price_idx "
            f"FROM index_levels WHERE variant = '{cfg['name']}'"
            for cfg in _VARIANT_CONFIGS if cfg["construction"] != "value")
        self.con.execute(
            f"""
            CREATE OR REPLACE TABLE nominal_decomp AS
            WITH v AS ({val_union}),
                 p AS ({price_union})
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
    def run(self, cfg_name: str) -> None:
        """Compute exactly ONE index named by its config (one config = one
        index). Builds the needed arm tables for its q_col on demand, then
        writes that single variant's level into index_levels."""
        cfg = _VARIANTS_BY_NAME[cfg_name]
        q = cfg["q_col"]
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS index_levels "
            "(trade_date DATE, variant VARCHAR, idx DOUBLE)")
        self._ensure_base_snapshot(q)
        self.value_index(q)
        self.fisher_fixed(q)
        self.fisher_chained(q)
        tbl, col = self._src_for(cfg)
        self.con.execute(
            "DELETE FROM index_levels WHERE variant = ?", [cfg["name"]])
        self.con.execute(
            f"""
            INSERT INTO index_levels
            SELECT trade_date, '{cfg['name']}' AS variant, {col} AS idx
            FROM {tbl}
            """
        )

    def run_all(self) -> None:
        """Compute all 18 variant configs. Running every config name gives you
        every index; running one config name gives you one index."""
        self.store.build_clean_panel(self.base_date, self.universe)
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS index_levels "
            "(trade_date DATE, variant VARCHAR, idx DOUBLE)")
        self.con.execute("DELETE FROM index_levels")
        for cfg in _VARIANT_CONFIGS:
            self.run(cfg["name"])
        self._qty_and_divisors()
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
