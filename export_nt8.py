"""Exports Databento ticks (MBO fills or trades) as a NinjaTrader 8 import file.

Format (Tools > Historical Data > Import, type "Tick"):
    File name   <Instrument>.Last.txt, e.g. "ES 12-25.Last.txt"
    Line        yyyyMMdd HHmmss fffffff;Price;Volume   (fffffff = 100 ns units)
Timestamps are written in UTC by default; then choose "UTC" as the time zone in the
import dialog (or use --tz Europe/Berlin and choose accordingly).

Usage:
    python export_nt8.py glbx-mdp3-20250922.mbo.dbn.zst [more days ...] --out nt8
    python export_nt8.py --day 2025-09-22 2025-09-23 --schema mbo --data-dir PATH --symbol ESZ5
    python export_nt8.py --all --schema mbo --data-dir PATH --symbol ESZ5      (all days in the folder)
Multiple files are processed day by day and appended to one file per contract
(so memory usage stays at one day). Without --symbol each day picks the contract with the
most ticks; across a roll this produces two files (e.g. ES 09-25 and ES 12-25).
No ZIP: NinjaTrader's @TextImportType opens the file with a StreamReader and would
read an archive as text ("Import field separator could not be identified").
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from data_loader import MONTH_CODES, day_file, load_databento

NT_MONTHS = {code: i + 1 for i, code in enumerate(MONTH_CODES)}


def nt8_instrument(symbol: str) -> str:
    """'ESZ5.GLBX' -> 'ES 12-25' (NinjaTrader contract name)."""
    sym = symbol.split(".")[0]
    root, code, year = sym[:-2], sym[-2], sym[-1]
    return f"{root} {NT_MONTHS[code]:02d}-2{year}"


def write_nt8(ticks, path: Path, tz: str = "UTC", append: bool = False) -> int:
    """Writes ticks as an NT8 Last file (append=True appends); returns the number of lines."""
    zone = timezone.utc if tz.upper() == "UTC" else ZoneInfo(tz)
    n = 0
    with open(path, "a" if append else "w", encoding="ascii", newline="\n") as f:
        for t in ticks:
            ns = t.ts_event
            dt = datetime.fromtimestamp(ns // 1_000_000_000, tz=zone)
            f.write(f"{dt:%Y%m%d %H%M%S} {(ns % 1_000_000_000) // 100:07d};{float(t.price):g};{int(t.size)}\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Databento -> NinjaTrader 8 Historical Data Import (Tick/Last)")
    ap.add_argument("files", nargs="*", help="DBN files (mbo = fills like NT ticks, trades = aggressor records)")
    ap.add_argument("--day", nargs="+", metavar="YYYY-MM-DD", help="trading days instead of file paths")
    ap.add_argument("--all", action="store_true", help="all daily files in the --data-dir folder (schema --schema)")
    ap.add_argument("--schema", default="mbo", choices=["mbo", "trades"], help="schema for --day (default mbo)")
    ap.add_argument("--data-dir", default=".", help="folder of the daily files for --day")
    ap.add_argument("--symbol", help="contract, e.g. ESZ5 (default: most ticks)")
    ap.add_argument("--out", default="nt8", help="output folder (default ./nt8)")
    ap.add_argument("--tz", default="UTC", help="time zone of the timestamps in the file (choose the same in the NT import)")
    args = ap.parse_args()

    paths = list(args.files)
    for day in args.day or []:
        p = day_file(day, args.data_dir, args.schema)
        if not p.exists():
            sys.exit(f"No file for {day}: {p}")
        paths.append(str(p))
    if args.all:
        paths += [str(p) for p in sorted(Path(args.data_dir).glob(f"glbx-mdp3-*.{args.schema}.dbn.zst"))]
    if not paths:
        sys.exit("No input files (give paths, --day or --all).")

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    written: dict[str, list] = {}     # NT name -> [lines, first ts, last ts, path]
    for path in paths:                # day by day, so only one day is held in memory
        try:
            instrument, ticks = load_databento(path, symbol=args.symbol)
        except ValueError as e:
            print(f"skipped {Path(path).name}: {e}")
            continue
        if not ticks:
            print(f"skipped {Path(path).name}: no ticks")
            continue
        name = nt8_instrument(str(instrument.id))
        txt = out / f"{name}.Last.txt"
        first_file = name not in written
        n = write_nt8(ticks, txt, args.tz, append=not first_file)
        rec = written.setdefault(name, [0, ticks[0].ts_event, ticks[-1].ts_event, txt])
        rec[0] += n; rec[2] = ticks[-1].ts_event
        print(f"{Path(path).name}: {instrument.id} {n} ticks -> {txt.name}")
    if not written:
        sys.exit("No ticks written.")
    for name, (n, t0, t1, txt) in written.items():
        first = datetime.fromtimestamp(t0 / 1e9, tz=timezone.utc); last = datetime.fromtimestamp(t1 / 1e9, tz=timezone.utc)
        print(f"\n{txt}: {n} ticks, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC, timestamps in {args.tz}")
        print(f"  NT8: Tools > Historical Data > Import, type Tick, time zone {args.tz} - instrument '{name}' must "
              "exist in NT. Import the .txt uncompressed (no ZIP).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
