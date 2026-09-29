"""Exports bars built from Databento data like NinjaTrader's TickBarsExporter indicator.

Same files as the indicator (Documents/NinjaTrader 8/bin/Custom/Indicators/TickBarsExporter.cs),
so both can be compared line by line:
    File name   <out stem>_<instrument>_<period>_<yyyy-MM-dd>.csv,
                e.g. Bar_Export_ESMAR25_2000 - Tick_2025-01-02.csv
    Header      DateTime|Open|High|Low|Close|Volume
    Line        2025-01-02 00:04:26|5949,25|5953,25|5943,25|5952,75|3050
                bar time = time of the last tick (seconds truncated) in --tz, prices with
                decimal comma as NT writes them on a German system, UTF-8 with BOM, CRLF.
One file per date of the bar time. Bars restart at the CME session start (17:00 Chicago);
the partial last bar of a session is exported, the bar still forming at the end of the data is not.
Existing files are only extended with bars newer than their last line (like ProtectExistingData);
--overwrite rewrites them instead.

A session needs the previous file(s) for its start (00:00-01:00 Berlin lies in the previous
UTC file); they are loaded automatically. Use MBO data for tick bars (NT tick = Databento fill).

Usage:
    python export_bars.py --day 2025-01-02 --data-dir D:\\trading\\GLBX-20260926-JAN_MAI --symbol ESH5
    python export_bars.py --all --data-dir PATH --symbol ESH5 --bars range:4 --out G:\\...\\Bar_Export.csv
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data_loader import day_file, load_databento, previous_day_files
from export_nt8 import nt8_instrument
from range_bars import make_aggregator
from sniper import trading_day

HEADER = "DateTime|Open|High|Low|Close|Volume"
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
PERIOD_TYPES = {"tick": "Tick", "range": "Range", "vol": "Volume", "volume": "Volume"}


def nt_fullname(symbol: str) -> str:
    """'ESH5.GLBX' -> 'ESMAR25' (Instrument.FullName 'ES MAR25' without spaces, as in the indicator)."""
    root, rest = nt8_instrument(symbol).split(" ")
    month, year = rest.split("-")
    return f"{root}{MONTHS[int(month) - 1]}{year}"


def nt_period(spec: str) -> str:
    """'tick:2000' -> '2000 - Tick' (BarsPeriod.Value + ' - ' + BarsPeriodType)."""
    kind, _, n = spec.partition(":")
    return f"{n} - {PERIOD_TYPES[kind]}"


def nt_number(x: float, decimal: str = ",") -> str:
    """Like double.ToString() in NT: shortest form, no trailing zeros (5850, 5853,5)."""
    s = repr(round(x, 10))
    if s.endswith(".0"):
        s = s[:-2]
    return s.replace(".", decimal)


def bar_line(b, zone, decimal: str) -> tuple[datetime, str]:
    t = datetime.fromtimestamp(b.ts_close // 1_000_000_000, tz=zone)
    return t, (f"{t:%Y-%m-%d %H:%M:%S}|{nt_number(b.open, decimal)}|{nt_number(b.high, decimal)}|"
               f"{nt_number(b.low, decimal)}|{nt_number(b.close, decimal)}|{int(round(b.volume))}")


def last_stamp(path: Path) -> str | None:
    """Newest bar time already in the file (lines start with the sortable time stamp)."""
    stamps = [ln.split("|", 1)[0] for ln in path.read_text(encoding="utf-8-sig").splitlines()[1:] if "|" in ln]
    return max(stamps) if stamps else None


def write_date_file(path: Path, lines: list[tuple[datetime, str]], overwrite: bool) -> tuple[int, int]:
    """Create or extend at the end only (strictly newer bars); returns (added, skipped)."""
    lines = sorted(lines, key=lambda x: x[0])
    if overwrite or not path.exists():
        with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write(HEADER + "\n")
            f.writelines(ln + "\n" for _, ln in lines)
        return len(lines), 0
    last = last_stamp(path)
    new = [ln for t, ln in lines if last is None or f"{t:%Y-%m-%d %H:%M:%S}" > last]
    if new:
        with open(path, "a", encoding="utf-8", newline="\r\n") as f:
            f.writelines(ln + "\n" for ln in new)
    return len(new), len(lines) - len(new)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bars as NinjaTrader TickBarsExporter CSV files")
    ap.add_argument("--day", nargs="+", metavar="YYYY-MM-DD", help="trading days to export")
    ap.add_argument("--all", action="store_true", help="all daily files in --data-dir")
    ap.add_argument("--data-dir", default=".", help="folder of the Databento daily files")
    ap.add_argument("--schema", default="mbo", choices=["mbo", "trades"], help="default mbo (NT tick = fill)")
    ap.add_argument("--symbol", help="contract, e.g. ESH5 (default: most ticks on the first day)")
    ap.add_argument("--bars", default="tick:2000", help="tick:N, range:N or vol:N (default tick:2000)")
    ap.add_argument("--out", default="bar_export/Bar_Export.csv", help="export path like the indicator's ExportPath")
    ap.add_argument("--tz", default="Europe/Berlin", help="time zone of the bar times (NT's time zone)")
    ap.add_argument("--decimal", default=",", help="decimal separator (NT writes the system culture's)")
    ap.add_argument("--overwrite", action="store_true", help="rewrite existing date files instead of appending")
    args = ap.parse_args()

    if args.all:
        days = sorted({date(int(p.name[10:14]), int(p.name[14:16]), int(p.name[16:18]))
                       for p in Path(args.data_dir).glob(f"glbx-mdp3-*.{args.schema}.dbn.zst")})
    else:
        days = sorted(date.fromisoformat(d) for d in args.day or [])
    if not days:
        sys.exit("No days (give --day or --all).")
    files: set[Path] = set()
    for d in days:
        p = day_file(d.isoformat(), args.data_dir, args.schema)
        if not p.exists():
            sys.exit(f"No file for {d}: {p}")
        files.add(p)
        prev = previous_day_files(d.isoformat(), args.data_dir, args.schema)
        if not prev and not args.all:
            print(f"Warning: no previous file for {d}, its session start (before 00:00 UTC) is missing")
        files.update(prev)

    zone = ZoneInfo(args.tz)
    wanted = set(days)
    agg, symbol, closed = None, args.symbol, []
    for path in sorted(files):                       # one file in memory at a time
        try:
            instrument, ticks = load_databento(path, symbol=symbol)
        except ValueError as e:
            print(f"skipped {path.name}: {e}")
            continue
        symbol = symbol or str(instrument.id).split(".")[0]
        if agg is None:
            agg = make_aggregator(args.bars, float(instrument.price_increment))
        for t in ticks:
            closed += agg.update(float(t.price), float(t.size), t.ts_event)
        # The file reaches 00:00 UTC of the next day: a session that ended before that is over,
        # so its partial last bar is closed like NT does at the session end (e.g. Friday).
        file_end = int(datetime.fromisoformat(path.name[10:18]).replace(tzinfo=ZoneInfo("UTC")).timestamp() * 1e9) \
            + 86_400 * 10**9
        if agg.next_session <= file_end:
            closed += agg.end_session()

    by_date: dict[str, list] = defaultdict(list)
    for b in closed:
        if trading_day(b.ts_open) in wanted:
            t, line = bar_line(b, zone, args.decimal)
            by_date[f"{t:%Y-%m-%d}"].append((t, line))
    if not by_date:
        sys.exit("No bars exported.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = f"{out.stem}_{nt_fullname(symbol)}_{nt_period(args.bars)}"
    for d, lines in sorted(by_date.items()):
        path = out.parent / f"{stem}_{d}{out.suffix}"
        added, skipped = write_date_file(path, lines, args.overwrite)
        note = f", {skipped} skipped (not newer than the last stored bar)" if skipped else ""
        print(f"{path.name}: {len(lines)} bars, +{added} written{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
