-- Nominal Values Index pipeline (DuckDB-Wasm)
-- S&P DJI divisor continuity + chained Fisher price/quantity decomposition.
-- Mirrors src/analytics/index_math.py; drop into SQL Lab.

CREATE OR REPLACE TABLE params AS
SELECT DATE '2015-01-02' AS base_date, 1000.0 AS base_level, 'SP500' AS universe;

CREATE OR REPLACE TABLE idx_panel AS
SELECT p.ticker, p.trade_date, p.adj_close AS p_t,
       sc.shares_outstanding * sc.iwf AS q_t,
       sc.shares_outstanding * sc.iwf * p.adj_close AS mv_t,
       t.sleeve_tag
FROM cleaned_prices p
JOIN share_counts sc
  ON sc.ticker = p.ticker AND sc.as_of = (
       SELECT MAX(as_of) FROM share_counts sc2
       WHERE sc2.ticker = sc.ticker AND sc2.as_of <= p.trade_date)
JOIN sp500_tags t ON t.ticker = p.ticker
WHERE p.trade_date >= (SELECT base_date FROM params)
  AND t.sleeve_tag = (SELECT universe FROM params);

CREATE OR REPLACE TEMP TABLE base_snap AS
SELECT ticker, p_t AS p_0, q_t AS q_0, mv_t AS mv_0
FROM idx_panel WHERE trade_date = (SELECT base_date FROM params);

CREATE OR REPLACE TABLE daily_mv AS
SELECT trade_date, SUM(mv_t) AS mv_t FROM idx_panel GROUP BY trade_date;

CREATE OR REPLACE TABLE value_index AS
WITH d0 AS (
  SELECT mv_t / (SELECT base_level FROM params) AS d_0
  FROM daily_mv WHERE trade_date = (SELECT base_date FROM params))
SELECT m.trade_date, m.mv_t / (SELECT d_0 FROM d0) AS value_idx
FROM daily_mv m;

CREATE OR REPLACE TABLE fisher_arms AS
SELECT p.trade_date,
       SUM(p.p_t * b.q_0)                                AS L_num,
       (SELECT SUM(p_0*q_0) FROM base_snap)             AS L_den,
       SUM(p.mv_t)                                       AS P_num,
       SUM(p.p_0 * p.q_t)                                AS P_den
FROM idx_panel p JOIN base_snap b ON b.ticker = p.ticker
GROUP BY p.trade_date;

CREATE OR REPLACE TABLE fisher_price AS
WITH arms AS (
  SELECT trade_date, L_num/L_den AS L_raw, P_num/P_den AS P_raw
  FROM fisher_arms),
base AS (
  SELECT L_raw AS L_0, P_raw AS P_0 FROM arms
  WHERE trade_date = (SELECT base_date FROM params))
SELECT a.trade_date,
  SQRT((a.L_raw/(SELECT L_0 FROM base)*(SELECT base_level FROM params)) *
       (a.P_raw/(SELECT P_0 FROM base)*(SELECT base_level FROM params))) AS fisher_price_idx
FROM arms a;

CREATE OR REPLACE TABLE fisher_qty AS
SELECT v.trade_date, v.value_idx / f.fisher_price_idx AS fisher_qty_idx
FROM value_index v JOIN fisher_price f ON f.trade_date = v.trade_date;

CREATE OR REPLACE TABLE nominal_decomp AS
SELECT v.trade_date,
       v.value_idx AS nominal_idx,
       f.fisher_price_idx AS price_idx,
       q.fisher_qty_idx AS qty_idx,
       v.value_idx / LAG(v.value_idx) OVER w - 1        AS ret_total,
       f.fisher_price_idx / LAG(f.fisher_price_idx) OVER w - 1 AS ret_price,
       q.fisher_qty_idx / LAG(q.fisher_qty_idx) OVER w - 1 AS ret_qty
FROM value_index v
JOIN fisher_price f ON f.trade_date = v.trade_date
JOIN fisher_qty q ON q.trade_date = v.trade_date
WINDOW w AS (ORDER BY v.trade_date);

-- Divisor event update (run at event time t*):
--   k   := MV_after / MV_before
--   additive (S&P eq.7): D_new = D_old + (MV_after-MV_before)/index_level
--   Fisher arms: UPDATE fisher_arms SET L_den=L_den*k, P_den=P_den*k;
--   then re-run value_index / fisher_price / decompose blocks above.
