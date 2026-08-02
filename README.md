# stockmagic

StockMonitor suite — market-data capture + S&P DJI index mathematics with a
chained Fisher price/quantity decomposition and quality/value dual-pass gates.

## What's here

```
src/data/market_data.py      Raw feed capture (trades, prices, shares, sleeves)
                             + audited clean panel (schema/jump/flatline).
src/analytics/index_math.py  S&P divisor value index + dual-arm Fisher price,
                             derived Fisher qty, nominal decomposition, event
                             divisor updates.
src/analytics/quality_value.py  Buffett/trifecta/leverage/DuPont gates.
sql/nominal_index_pipeline.sql  Same math as a DuckDB-Wasm SQL script for SQL Lab.
tests/test_index_math.py     Synthetic-data smoke test (no external deps).
```

## Design notes (from the S&P DJI methodology critique)

- The cap-weighted index is a **Laspeyres-derived, base-scaled** index. We keep
  S&P's divisor as the *continuity engine* for the aggregate market value.
- On top we hang a **Fisher price index** (geo mean of Laspeyres + Paasche arms),
  each arm with its own divisor so continuity + Fisher symmetry hold.
- **Fisher quantity** is derived as `V / P_F`; it inherits continuity for free.
- `Nominal = P_F * Q_F` by construction → every period return splits cleanly
  into a valuation (price) component and a share/cap (quantity) component — the
  building block for the "Fisher quantity sleeve" in the macro layer.
- Divisor moves only on **non-market events** (add/delete, corp actions, share
  or IWF changes). The cap/AWF residual is logged separately as "capping drag".

## Run

```bash
pip install duckdb
python -m pytest tests/ -q
# or just the smoke test:
python tests/test_index_math.py
```
