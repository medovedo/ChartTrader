"""Runs the EntryCount and DoubleTrap ports over a whole data set and writes their event CSVs
(same format as the NT indicators, file names with the suffix _CT).

Input A - ChartTrader bars from Databento (MBO = NT ticks, see CLAUDE.md):
    python pa_run.py --day 2026-10-09 --data-dir D:\\trading\\GLBX-... --symbol ESZ6
    (the previous trading day is loaded as warm-up like in the chart; --warmup-days N for more)
Input B - NinjaTrader bar export (TickBarsExporter), the same bars NT used:
    python pa_run.py --nt-bars "G:\\Meine Ablage\\Trading\\Bar_Export_ESDEC26_2000 - Tick_2026-10-0*.csv"
    or the Bar lines of NT's own EntryCount CSVs (exactly NT's bars, best for the parity check):
    python pa_run.py --nt-bars "Documents/NinjaTrader 8/EntryCount_ES_DEC26_2026100[6-9].csv" --nt-ticks "ES 12-26.Last.txt"
    Ticks for the intrabar order of outside bars (otherwise bar color, as NT without tick data):
    --nt-ticks FILE (NT tick export) or --tick-data-dir DIR --symbol ESZ6 (Databento)

The CSVs go to pa_csv/ (entrycount_doubletrap.toml, [ChartTrader] CsvDir) or --out.
Compare with NT: python parity_check.py --day 20261009
"""
from __future__ import annotations

import argparse
import glob
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from entrycount_doubletrap_config import load_config
from pa_runner import (PaRunner, instrument_from_export_name, instrument_from_symbol, is_event_csv, read_bar_export,
                       read_event_csv_bars, read_nt_ticks)


def expand(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        out.extend(Path(h) for h in (hits or [p]))
    return out


def databento_files(days: list[date], data_dir: str, schema: str, warmup_days: int) -> list[Path]:
    from data_loader import day_file, previous_day_files
    files: set[Path] = set()
    first = min(days)
    for d in days:
        p = day_file(d.isoformat(), data_dir, schema)
        if not p.exists():
            sys.exit(f"No file for {d}: {p}")
        files.add(p)
    start = first
    for _ in range(max(1, warmup_days)):
        prev = previous_day_files(start.isoformat(), data_dir, schema)
        if not prev:
            print(f"Warning: no previous file before {start} (warm-up incomplete)")
            break
        files.update(prev)
        start = date(int(prev[0].name[10:14]), int(prev[0].name[14:16]), int(prev[0].name[16:18]))
    return sorted(files)


def run_input_a(args, cfg) -> PaRunner:
    from data_loader import load_databento
    from range_bars import make_aggregator
    days = [date.fromisoformat(d) for d in args.day]
    files = databento_files(days, args.data_dir, args.schema, args.warmup_days)
    runner, agg, symbol = None, None, args.symbol
    for path in files:                                   # one file in memory at a time
        try:
            instrument, ticks = load_databento(path, symbol=symbol)
        except ValueError as e:
            print(f"skipped {path.name}: {e}")
            continue
        symbol = symbol or str(instrument.id).split(".")[0]
        if runner is None:
            name = args.instrument or instrument_from_symbol(symbol)
            runner = PaRunner(cfg, name, export_csv=True, csv_dir=args.out)
            agg = make_aggregator(args.bars, float(instrument.price_increment))
            print(f"Input A: {symbol} ({name}), {args.bars}")
        for t in ticks:
            price, ts = float(t.price), t.ts_event
            runner.on_tick(price, ts, agg.update(price, float(t.size), ts))
        # A session that ended within this file closes its partial last bar (as NT does at the session end)
        file_end = int(datetime.fromisoformat(path.name[10:18]).replace(tzinfo=timezone.utc).timestamp() * 1e9) \
            + 86_400 * 10**9
        if agg.next_session <= file_end:
            for b in agg.end_session():
                runner.close_bar(b.ts_close // runner.resolution_ns, runner.to_pabar(b))
        print(f"  {path.name}: {len(ticks)} ticks, {runner.bar_count} bars so far")
    if runner is None:
        sys.exit("No data loaded.")
    return runner


def run_input_b(args, cfg) -> PaRunner:
    paths = expand(args.nt_bars)
    missing = [p for p in paths if not p.exists()]
    if missing:
        sys.exit(f"Not found: {', '.join(map(str, missing))}")
    if all(is_event_csv(p) for p in paths):
        bars = read_event_csv_bars(paths, cfg.chart.TimeZone)
        print(f"Input B: {len(paths)} NT event CSV(s), {len(bars)} Bar lines (exactly NT's bars)")
    else:
        bars, rep = read_bar_export(paths, cfg.chart.TimeZone)
        print(f"Input B: {len(paths)} file(s), {rep.rows} rows, {rep.duplicates} duplicate(s) removed, {len(bars)} bars")
        if rep.backwards:
            print(f"Warning: {rep.backwards} row(s) go back in time (reload with a playback jump?) - kept as they are")
    if not bars:
        sys.exit("No bars in the input.")
    name = args.instrument or instrument_from_export_name(paths[0]) or "Unknown"
    ticks = None
    if args.nt_ticks:
        t0 = int(bars[0].time.timestamp() * 1e9) - 86_400 * 10**9
        t1 = int(bars[-1].time.timestamp() * 1e9) + 10**9
        ticks = read_nt_ticks(expand(args.nt_ticks), t0, t1)
        print(f"NT ticks for the intrabar order: {len(ticks)}")
    elif args.tick_data_dir:
        from data_loader import day_file, load_databento
        first = bars[0].time.astimezone(timezone.utc).date() - timedelta(days=1)
        last = bars[-1].time.astimezone(timezone.utc).date()
        files = [day_file((first + timedelta(days=i)).isoformat(), args.tick_data_dir, args.schema)
                 for i in range((last - first).days + 1)]
        files = [f for f in files if f.exists()]
        ticks = []
        for f in files:
            _, tk = load_databento(f, symbol=args.symbol)
            ticks.extend((t.ts_event, float(t.price)) for t in tk)
        ticks.sort(key=lambda x: x[0])
        print(f"Ticks for the intrabar order: {len(ticks)} from {len(files)} file(s)")
    runner = PaRunner(cfg, name, export_csv=True, csv_dir=args.out)
    runner.run_bars(bars, ticks)
    return runner


def main() -> int:
    ap = argparse.ArgumentParser(description="EntryCount/DoubleTrap port: event CSVs like the NT indicators")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--day", nargs="+", metavar="YYYY-MM-DD", help="input A: Databento day(s)")
    src.add_argument("--nt-bars", nargs="+", metavar="CSV", help="input B: NT bar export(s) or NT EntryCount CSV(s) (its Bar lines), glob allowed")
    ap.add_argument("--data-dir", default=".", help="input A: folder of the Databento daily files")
    ap.add_argument("--schema", default="mbo", choices=["mbo", "trades"], help="Databento schema (default mbo)")
    ap.add_argument("--symbol", help="contract, e.g. ESZ6 (default: most ticks)")
    ap.add_argument("--bars", default="tick:2000", help="input A: bar type (default tick:2000)")
    ap.add_argument("--warmup-days", type=int, default=1, help="input A: previous trading days as warm-up (default 1)")
    ap.add_argument("--nt-ticks", nargs="+", metavar="TXT",
                    help="input B: NT tick export (Historical Data > Export, Last, UTC) for the intrabar order")
    ap.add_argument("--tick-data-dir", help="input B: Databento folder for the intrabar order of outside bars")
    ap.add_argument("--instrument", help="instrument name in the file names, e.g. 'ES DEC26'")
    ap.add_argument("--config", help="parameter file (default entrycount_doubletrap.toml)")
    ap.add_argument("--out", help="output folder (default [ChartTrader] CsvDir)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    runner = run_input_a(args, cfg) if args.day else run_input_b(args, cfg)
    files = runner.finish()
    print(f"{runner.bar_count} bars processed")
    for f in files:
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
