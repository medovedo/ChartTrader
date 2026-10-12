"""Trade journal: one CSV row per closed position (from Nautilus' PositionClosed event).

In NETTING mode a position is closed when it is flat again, so a Sniper trade with Target1 and
runner is one row (exit = average of all exits). The cache only keeps the last closed position
(the position ID is reused), which is why the rows come from the events.

The file is recreated (header only) at every start, then appended to row by row (survives a crash)
and is meant for Excel on a German system:
separator ';', decimal comma, UTF-8 with BOM. Times in Europe/Berlin.

The detail columns behind `Duration` come from a `TradeInfo`: the Sniper fills it when the trap is
armed (signal bar, trigger, limit, structural stop, ATM) and with the planned targets after the fill;
ManualStrategy adds the exit fills and the confirmed manual order moves. `Surprised` = the GUI box was
ticked when the position opened (ticking it later does not change that trade). A file that still has the
old short header is left alone; the log then goes to `<name>_v2.csv`.

A setup that ends without a position (trap expired, entry not filled, Cancel All) gets a row of its own
(`record_no_trade`): Qty 0, no entry/exit/PnL, Open = armed, Close = ended, `RunnerExitReason` =
"NoTrade (<reason>)", `Surprised` = state of the box when the setup ended.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

COLUMNS_V1 = ["Replayed", "Date", "Open", "Close", "Instrument", "Setup", "Side", "Qty",
              "Entry", "Exit", "Points", "Ticks", "PnL", "Duration"]
DETAIL_COLUMNS = ["SignalBarStart", "SignalO", "SignalH", "SignalL", "SignalC", "PrevBarH", "PrevBarL",
                  "Trigger", "EntryType", "LimitPrice", "InitialSL", "RiskTicks", "Template", "MaxRiskTicks",
                  "T1Price", "T2Price", "ExitT1", "ExitT1Time", "ExitRunner", "ExitRunnerTime",
                  "RunnerExitReason", "ManualChanges", "DistEMA", "Surprised"]
COLUMNS = COLUMNS_V1 + DETAIL_COLUMNS


@dataclass
class ExitFill:
    px: float
    qty: float
    ts: int
    role: str             # "T1", "T2", "SL", "SL1", "SL2" or "" (flatten, other manual order)
    is_stop: bool
    trigger: float = 0.0  # stop trigger at fill time (breakeven detection)


@dataclass
class TradeInfo:
    """Details of one trade for the journal; every field may stay empty (manual entry without trap)."""
    entry_type: str = "Manual"               # Direct / PullbackLimit / Manual
    signal_ts: int | None = None             # ts_open of the signal bar (ns)
    signal_ohlc: tuple[float, float, float, float] | None = None
    prev_hl: tuple[float, float] | None = None
    trigger: float | None = None
    limit: float | None = None               # only for PullbackLimit
    initial_sl: float | None = None
    template: str = ""
    max_risk: int | None = None
    dist_ema: float | None = None            # signal bar close - EMA, points
    targets: list = field(default_factory=list)          # planned T1, T2 (aligned to the fill)
    legs: dict = field(default_factory=dict)             # order ID -> role (manual bracket)
    trade: Any = None                                    # Sniper AtmTrade (roles follow replaced exits)
    exits: list[ExitFill] = field(default_factory=list)
    manual_changes: list[str] = field(default_factory=list)
    surprised: bool = False                  # "Surprised" box was ticked when the position opened
    armed_ts: int | None = None              # trap armed / entry placed (ns), for rows without a trade


def ema_last(closes: list[float], period: int) -> float | None:
    """Last EMA value as drawn in the chart (NinjaTrader: start value = first close)."""
    if not closes or period <= 0:
        return None
    k, e = 2.0 / (period + 1), closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def _num(x: float, decimal: str) -> str:
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return (s if s not in ("", "-0") else "0").replace(".", decimal)


def _px(x: float | None, decimal: str) -> str:
    return "" if x is None else f"{x:.2f}".replace(".", decimal)


def _avg(fills: list[ExitFill]) -> float | None:
    qty = sum(f.qty for f in fills)
    return sum(f.px * f.qty for f in fills) / qty if qty > 0 else None


def _duration(ns: int) -> str:
    s = ns // 1_000_000_000
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _has_header(path: Path, columns: list[str], sep: str) -> bool:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.readline().rstrip("\r\n") == sep.join(columns)
    except OSError:
        return False


class TradeJournal:
    def __init__(self, path: str | Path, tick_size: float, tz: str = "Europe/Berlin",
                 sep: str = ";", decimal: str = ","):
        self.path = Path(path)
        if _has_header(self.path, COLUMNS_V1, sep):          # keep logs of the old format
            self.path = self.path.with_name(f"{self.path.stem}_v2{self.path.suffix}")
        self.tick = tick_size
        self.zone = ZoneInfo(tz)
        self.sep, self.decimal = sep, decimal
        self.replayed = datetime.now().strftime("%Y-%m-%d %H:%M")   # identifies this replay run
        self.rows: list[dict] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:   # fresh log per start
            f.write(self.sep.join(COLUMNS) + "\r\n")

    def record(self, event, setup: str, info: TradeInfo | None = None) -> dict:
        """Append a row for a PositionClosed event; returns the row."""
        sign = 1 if event.entry.name == "BUY" else -1
        entry, exit_ = float(event.avg_px_open), float(event.avg_px_close)
        points = (exit_ - entry) * sign
        t0 = datetime.fromtimestamp(event.ts_opened / 1e9, tz=self.zone)
        t1 = datetime.fromtimestamp(event.ts_closed / 1e9, tz=self.zone)
        d = self.decimal
        row = {
            "Replayed": self.replayed,
            "Date": f"{t0:%Y-%m-%d}",
            "Open": f"{t0:%H:%M:%S}",
            "Close": f"{t1:%H:%M:%S}",
            "Instrument": str(event.instrument_id).split(".")[0],
            "Setup": setup,
            "Side": "LONG" if sign > 0 else "SHORT",
            "Qty": f"{float(event.peak_qty):g}",
            "Entry": _num(entry, d),
            "Exit": _num(round(exit_, 2), d),          # average over partial exits, e.g. 5957,67
            "Points": _num(round(points, 2), d),
            "Ticks": _num(round(points / self.tick, 2), d),
            "PnL": _num(round(float(event.realized_pnl), 2), d),
            "Duration": _duration(event.duration_ns),
        }
        row.update(dict.fromkeys(DETAIL_COLUMNS, ""))
        try:
            row.update(self._details(info or TradeInfo(), entry))
        except Exception as e:      # a detail column must never cost the row
            row["ManualChanges"] = f"detail error: {e}".replace(self.sep, ",")
        self._append(row)
        return row

    def record_no_trade(self, instrument: str, setup: str, is_long: bool, info: TradeInfo | None,
                        reason: str, ts_end: int, surprised: bool) -> dict:
        """Append a row for a setup that ended without a position; returns the row."""
        info = info or TradeInfo()
        t0 = datetime.fromtimestamp((info.armed_ts or ts_end) / 1e9, tz=self.zone)
        row = dict.fromkeys(COLUMNS, "")
        row.update({
            "Replayed": self.replayed,
            "Date": f"{t0:%Y-%m-%d}",
            "Open": f"{t0:%H:%M:%S}",
            "Close": self._time(ts_end),
            "Instrument": instrument,
            "Setup": setup,
            "Side": "LONG" if is_long else "SHORT",
            "Qty": "0",
        })
        try:
            planned = info.limit if info.limit is not None else info.trigger   # order price of the entry
            out = self._details(info, planned if planned is not None else 0.0)
            if planned is None:
                out.pop("RiskTicks", None)
            row.update(out)
        except Exception as e:
            row["ManualChanges"] = f"detail error: {e}".replace(self.sep, ",")
        row["RunnerExitReason"] = f"NoTrade ({reason})"
        row["Surprised"] = "yes" if surprised else "no"
        self._append(row)
        return row

    def _append(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            f.write(self.sep.join(row[c] for c in COLUMNS) + "\r\n")
        self.rows.append(row)

    def _details(self, info: TradeInfo, entry: float) -> dict:
        d, out = self.decimal, {}
        if info.signal_ts is not None:
            out["SignalBarStart"] = self._time(info.signal_ts)
        if info.signal_ohlc is not None:
            for k, v in zip(("SignalO", "SignalH", "SignalL", "SignalC"), info.signal_ohlc):
                out[k] = _px(v, d)
        if info.prev_hl is not None:
            out["PrevBarH"], out["PrevBarL"] = _px(info.prev_hl[0], d), _px(info.prev_hl[1], d)
        out["Trigger"] = _px(info.trigger, d)
        out["EntryType"] = info.entry_type
        out["LimitPrice"] = _px(info.limit, d)
        out["InitialSL"] = _px(info.initial_sl, d)
        if info.initial_sl is not None:
            out["RiskTicks"] = _num(round(abs(entry - info.initial_sl) / self.tick, 2), d)
        out["Template"] = info.template
        out["MaxRiskTicks"] = "" if info.max_risk is None else str(info.max_risk)
        for k, v in zip(("T1Price", "T2Price"), info.targets):
            out[k] = _px(v, d)
        t1 = [f for f in info.exits if f.role == "T1" and not f.is_stop]
        rest = [f for f in info.exits if not (f.role == "T1" and not f.is_stop)]
        if t1:
            out["ExitT1"] = _px(_avg(t1), d)
            out["ExitT1Time"] = self._time(t1[-1].ts)
        if rest:
            out["ExitRunner"] = _px(_avg(rest), d)
            out["ExitRunnerTime"] = self._time(rest[-1].ts)
        if info.exits:
            out["RunnerExitReason"] = self._reason(info.exits[-1], entry)
        out["ManualChanges"] = " | ".join(info.manual_changes).replace(self.sep, ",")
        if info.dist_ema is not None:
            out["DistEMA"] = _px(info.dist_ema, d)
        out["Surprised"] = "yes" if info.surprised else "no"
        return out

    def _time(self, ts: int) -> str:
        return f"{datetime.fromtimestamp(ts / 1e9, tz=self.zone):%H:%M:%S}"

    def _reason(self, f: ExitFill, entry: float) -> str:
        """How the position was finally closed. The replay has no automatic session close,
        so a flatten at the end of the session counts as Manual."""
        if f.is_stop:
            return "BE" if self.is_breakeven(f.trigger, entry) else "Stop"
        if f.role.startswith("T"):
            return "Target"
        return "Manual"

    def is_breakeven(self, stop: float, entry: float) -> bool:
        """Stop at the average entry (within one tick: the entry can be averaged over several fills)."""
        return abs(stop - entry) < self.tick
