"""Generate stock_monitor's chained Fisher index over the SAME SP500 sleeve that
stockmagic bridges (sp500_member in monitored_stocks), into a reconciliation-only
file so we don't clobber the user's portfolio fisher_indexes.parquet.

Outputs: stock_monitor/fisher_indexes_sp500.parquet  (cols: date, fisher_p,
laspeyres_p, paasche_p — base 100, daily, DATE).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "stock_monitor"))

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import fisher_index as fi
from index_registry import tickers_for_index

DATA = Path(__file__).parent / "stock_monitor"
OUT = DATA / "fisher_indexes_sp500.parquet"


def main() -> None:
    tickers = tickers_for_index("sp500")
    print(f"sp500 sleeve for fisher: {len(tickers)} tickers")
    if len(tickers) < 3:
        raise SystemExit("too few sp500 tickers resolved")
    idx = fi.run(list(tickers), freq="D", label="sp500")
    keep = ["date", "fisher_p", "laspeyres_p", "paasche_p"]
    idx = idx[[c for c in keep if c in idx.columns]]
    # date is already datetime.date (carried through from panel index)
    pq.write_table(pa.Table.from_pandas(idx, preserve_index=False), OUT)
    print(f"wrote {OUT}  rows={len(idx)}  "
          f"range={idx['date'].min()}..{idx['date'].max()}  "
          f"has_paasche={'paasche_p' in idx.columns}")


if __name__ == "__main__":
    main()
