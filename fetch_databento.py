"""Fetches ES data (trades or mbo) plus the instrument definition for a time range from Databento.

Usage:   DATABENTO_API_KEY=... python fetch_databento.py 2025-09-22 2025-09-23 ESZ5 --schema mbo
         python fetch_databento.py 2025-09-22 2025-09-23 ESZ5 --schema mbo --cost-only
Queries the cost first and downloads only after confirmation (or not at all with --cost-only).

Note: the trades schema contains one record per aggressor (action T). NinjaTrader counts
ticks per fill (action F, only in the MBO schema). For NT-equivalent tick bars use --schema mbo.
"""
import argparse
import os
import sys
from pathlib import Path

import databento as db

ap = argparse.ArgumentParser(description="Download Databento data (cost is shown first)")
ap.add_argument("start"); ap.add_argument("end")
ap.add_argument("symbol", nargs="?", default="ES.c.0", help="e.g. ESZ5, ES.FUT (parent) or ES.c.0 (front month)")
ap.add_argument("--schema", default="trades", choices=["trades", "mbo", "tbbo", "mbp-1"])
ap.add_argument("--cost-only", action="store_true", help="only show cost and size")
ap.add_argument("--out", default="data", help="output folder")
args = ap.parse_args()

key = os.environ.get("DATABENTO_API_KEY")
if not key:
    sys.exit("DATABENTO_API_KEY not set (PowerShell: $env:DATABENTO_API_KEY = 'db-...')")

stype = "continuous" if ".c." in args.symbol else "parent" if args.symbol.endswith(".FUT") else "raw_symbol"
client = db.Historical(key)
common = dict(dataset="GLBX.MDP3", symbols=[args.symbol], stype_in=stype, start=args.start, end=args.end)

cost = client.metadata.get_cost(schema=args.schema, **common)
size = client.metadata.get_billable_size(schema=args.schema, **common)
print(f"{args.schema} {args.symbol} {args.start}..{args.end}: ${cost:.2f}, {size / 1e6:.0f} MB uncompressed")
if args.cost_only or input("Download? [y/N] ").lower() not in ("y", "j"):
    sys.exit(0)

out = Path(args.out); out.mkdir(exist_ok=True)
client.timeseries.get_range(schema=args.schema, path=out / f"{args.symbol}-{args.schema}.dbn.zst", **common)
client.timeseries.get_range(schema="definition", path=out / f"{args.symbol}-definition.dbn.zst",
                            **{**common, "end": args.start})  # definition needed for one day only
print("saved to", out.resolve())
