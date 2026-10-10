"""Drives the EntryCount and DoubleTrap ports bar by bar, like NinjaTrader with Calculate.OnBarClose.

Both indicators get the same input: closed bars (`PaBar`) and, for the intrabar order of outside bars,
the ticks of the 1-tick series in NinjaTrader's processing order. NT processes the series by time stamp;
a tick with the closing time stamp of a bar is processed after that bar (comment in
EntryCount.ResolveIntrabarOrder). So a tick is fed before bar k if its time stamp (at `resolution_ns`)
is earlier than the bar's closing time stamp, otherwise after it.

Input A: ChartTrader bars (aggregator fed with Databento ticks) -> `on_tick(price, ts_ns, closed_bars)`.
Input B: NinjaTrader bar export (Bar_Export_*.csv, `read_bar_export`) -> `run_bars(bars, ticks)`.
Without ticks EntryCount falls back to the bar color rule, as NT does without tick data.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from double_trap import DoubleTrap
from entry_count import EntryCount
from entrycount_doubletrap_config import PaConfig
from pa_support import Drawings, PaBar

MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
FUTURE_MONTH_CODES = "FGHJKMNQUVXZ"


class PaRunner:
    """One EntryCount and one DoubleTrap (which hosts its own EntryCount, like in NT)."""

    def __init__(self, cfg: PaConfig, instrument_name: str | None = None, export_csv: bool | None = None,
                 csv_dir: str | Path | None = None, printer: Callable[[str], None] | None = None):
        ch = cfg.chart
        self.cfg = cfg
        self.zone = ZoneInfo(ch.TimeZone)
        self.resolution_ns = max(1, ch.TickTimeResolutionMs) * 1_000_000
        out = Path(csv_dir) if csv_dir is not None else Path(__file__).with_name(ch.CsvDir) \
            if not Path(ch.CsvDir).is_absolute() else Path(ch.CsvDir)
        ec_cfg, dt_cfg = cfg.entry_count, cfg.double_trap
        if export_csv is not None:
            ec_cfg = replace(ec_cfg, ExportCsv=export_csv)
            dt_cfg = replace(dt_cfg, ExportCsv=export_csv)
        self.ec_draw, self.dt_draw = Drawings(), Drawings()
        self.ec = EntryCount(ec_cfg, ch.TickSize, instrument_name, out, ch.CsvSuffix, self.ec_draw, printer)
        self.dt = DoubleTrap(dt_cfg, ch.TickSize, instrument_name, out, ch.CsvSuffix, self.dt_draw, printer)
        self._pending: deque[tuple[int, float]] = deque()   # ticks not yet fed: (time stamp at resolution, price)
        self.bar_count = 0

    # --- input A: streaming from the ChartTrader aggregator ----------------------------------
    def to_pabar(self, b) -> PaBar:
        """RangeBar/TickBar of range_bars.py -> PaBar (time = last tick, NT's time zone)."""
        t = datetime.fromtimestamp(b.ts_close / 1e9, tz=timezone.utc).astimezone(self.zone)
        return PaBar(b.open, b.high, b.low, b.close, t, b.volume)

    def on_tick(self, price: float, ts_ns: int, closed_bars) -> None:
        """Call after every aggregator update with the bars it closed."""
        self._pending.append((ts_ns // self.resolution_ns, price))
        for b in closed_bars:
            self.close_bar(b.ts_close // self.resolution_ns, self.to_pabar(b))

    def close_bar(self, t_close: int, bar: PaBar) -> None:
        pending = self._pending
        while pending and pending[0][0] < t_close:
            _, price = pending.popleft()
            self.ec.on_tick(price)
            self.dt.on_tick(price)
        self.ec.on_bar_update(bar)
        self.dt.on_bar_update(bar)
        self.bar_count += 1

    # --- input B: bar list (NT bar export), optional ticks -------------------------------------
    def run_bars(self, bars: list[PaBar], ticks: list[tuple[int, float]] | None = None,
                 tick_resolution_ns: int = 1_000_000_000) -> None:
        """ticks = sorted (ts_ns, price). The export has whole seconds, so ticks are compared at
        `tick_resolution_ns` (default 1 s): a tick goes before bar k if its second is earlier than the bar's."""
        it = iter(ticks or [])
        nxt = next(it, None)
        for bar in bars:
            t_close = int(bar.time.timestamp() * 1e9) // tick_resolution_ns
            while nxt is not None and nxt[0] // tick_resolution_ns < t_close:
                self.ec.on_tick(nxt[1])
                self.dt.on_tick(nxt[1])
                nxt = next(it, None)
            self.ec.on_bar_update(bar)
            self.dt.on_bar_update(bar)
            self.bar_count += 1

    def finish(self) -> list[Path]:
        """State.Terminated: closes the CSV files; returns the files written."""
        self.ec.terminate()
        self.dt.terminate()
        return [p for d in (self.ec.diag, self.dt.diag) if d is not None for p in d.paths]


# --------------------------------------------------------------------------- NT bar export (input B)
@dataclass
class ExportReport:
    rows: int = 0
    duplicates: int = 0
    backwards: int = 0       # rows whose time is earlier than the previous kept row (kept, reported)


def read_bar_export(paths: list[str | Path], tz: str = "Europe/Berlin") -> tuple[list[PaBar], ExportReport]:
    """Bar_Export_*.csv of TickBarsExporter: 'DateTime|Open|High|Low|Close|Volume', DateTime = bar END in NT's
    time zone, decimal comma. Files are read in the given order. A row is a duplicate (chart reload) if High, Low,
    Close and Volume equal the previous row and its time is at most 2 s later; the first one is kept."""
    zone = ZoneInfo(tz)
    bars: list[PaBar] = []
    rep = ExportReport()
    prev_raw = None
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("DateTime"):
                    continue
                parts = ln.split("|")
                if len(parts) < 6:
                    continue
                t = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
                o, h, l, c = (float(x.replace(",", ".")) for x in parts[1:5])
                v = float(parts[5].replace(",", "."))
                rep.rows += 1
                raw = (t, h, l, c, v)
                if prev_raw is not None and (h, l, c, v) == prev_raw[1:] \
                        and 0 <= (t - prev_raw[0]).total_seconds() <= 2:
                    rep.duplicates += 1
                    prev_raw = raw
                    continue
                prev_raw = raw
                if bars and t < bars[-1].time:
                    rep.backwards += 1
                bars.append(PaBar(o, h, l, c, t, v))
    return bars, rep


def is_event_csv(path: str | Path) -> bool:
    """True for an EntryCount/DoubleTrap event CSV (header starts with BarNo;BarEndTime)."""
    with open(path, encoding="utf-8-sig") as f:
        return f.readline().startswith("BarNo;BarEndTime;")


def read_event_csv_bars(paths: list[str | Path], tz: str = "Europe/Berlin") -> list[PaBar]:
    """The 'Bar' lines of NT's EntryCount CSV (CsvIncludeBars): exactly the bars the indicator processed,
    with the same time stamps. The best input for the parity check; the bar export can differ from them
    (another chart, a reload, times one second apart)."""
    zone = ZoneInfo(tz)
    bars: list[PaBar] = []
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            header = f.readline().rstrip("\r\n").split(";")
            col = {c: i for i, c in enumerate(header)}
            for ln in f:
                p = ln.rstrip("\r\n").split(";")
                if len(p) < len(header) - 1 or p[col["Event"]] != "Bar" or p[col["Indicator"]] != "EntryCount":
                    continue
                t = datetime.strptime(p[col["BarEndTime"]], "%d.%m.%Y %H:%M:%S").replace(tzinfo=zone)
                o, h, l, c = (float(p[col[k]].replace(",", ".")) for k in ("Open", "High", "Low", "Close"))
                bars.append(PaBar(o, h, l, c, t))
    return bars


def read_nt_ticks(paths: list[str | Path], start_ns: int | None = None,
                  end_ns: int | None = None) -> list[tuple[int, float]]:
    """NT tick export (Historical Data > Export, Last): 'yyyyMMdd HHmmss fffffff;Last;Bid;Ask;Vol' in UTC,
    fffffff = 100 ns units. Returns sorted (ts_ns, price), optionally limited to [start_ns, end_ns)."""
    out: list[tuple[int, float]] = []
    epoch = {}
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            for ln in f:
                if len(ln) < 24:
                    continue
                d = ln[:8]
                base = epoch.get(d)
                if base is None:
                    base = epoch[d] = int(datetime(int(d[:4]), int(d[4:6]), int(d[6:8]),
                                                   tzinfo=timezone.utc).timestamp()) * 1_000_000_000
                ts = (base + (int(ln[9:11]) * 3600 + int(ln[11:13]) * 60 + int(ln[13:15])) * 1_000_000_000
                      + int(ln[16:23]) * 100)
                if (start_ns is not None and ts < start_ns) or (end_ns is not None and ts >= end_ns):
                    continue
                out.append((ts, float(ln[24:ln.index(";", 24)])))
    out.sort(key=lambda x: x[0])
    return out


def instrument_from_export_name(path: str | Path) -> str | None:
    """'Bar_Export_ESDEC26_2000 - Tick_2026-10-09.csv' -> 'ES DEC26' (Instrument.FullName);
    'EntryCount_ES_DEC26_20261009.csv' -> 'ES_DEC26' (already the file name form)."""
    name = Path(path).name
    m = re.match(r"(?:EntryCount|DoubleTrap)_(.+)_\d{8}\.csv$", name)
    if m:
        return m.group(1)
    m = re.search(r"_([A-Z0-9]+?)((?:" + "|".join(MONTHS) + r")\d{2})_", name)
    return f"{m.group(1)} {m.group(2)}" if m else None


def instrument_from_symbol(symbol: str) -> str:
    """'ESZ6' / 'ESZ6.GLBX' -> 'ES DEC26' (Instrument.FullName as NT 8.1 names the CSV files)."""
    sym = symbol.split(".")[0]
    root, code, year = sym[:-2], sym[-2], sym[-1]
    return f"{root} {MONTHS[FUTURE_MONTH_CODES.index(code)]}2{year}"
