"""Port of the NinjaTrader 8 indicator EntryCount.cs (Documents\\NinjaTrader 8\\bin\\Custom\\Indicators).

Port, not an improvement: same order, same names (snake_case), same defaults (entrycount_doubletrap_config.py),
including every ASSUMPTION of the C# code. Observations about the C# code go to PORT_NOTES.md, not into the code.
Each function names its C# source. Line numbers refer to EntryCount.cs as of 2026-10-09 (1942 lines).

Mapping of the NinjaScript runtime:
- OnBarUpdate for the primary series (Calculate.OnBarClose) = `on_bar_update(bar)`, called once per closed bar.
- OnBarUpdate for the 1-tick series (BarsInProgress 1) = `on_tick(price)`. The caller feeds the ticks in NT order:
  ticks with a time stamp earlier than the bar's closing time stamp before the bar, the others after it
  (see pa_runner.py).
- Series<T>[0] = attributes of the current bar, Series<T>[1] where the code needs it = kept explicitly.
- Draw.* = `Drawings` (pa_support.py) with absolute bar indices; Print = `printer`; Alert is not ported
  (NT only raises alerts in State.Realtime, which a replay never reaches).

The shared building blocks of EntryCount.cs (used by DoubleTrap as well) come first, as in the C# file.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from entrycount_doubletrap_config import EntryCountConfig
from pa_support import DrawDot, DrawText, Drawings, PaBar, net_custom, net_datetime, net_g, trading_day_of

# --------------------------------------------------------------------------- shared enums (lines 45-86)
# Strings for the enums, int values where the C# code stores them in Series<int>.
RISK_NONE, RISK_DIRECT, RISK_PULLBACK_LIMIT, RISK_TOO_LARGE = 0, 1, 2, 3
RISK_CLASS_NAMES = {RISK_NONE: "None", RISK_DIRECT: "Direct", RISK_PULLBACK_LIMIT: "PullbackLimit",
                    RISK_TOO_LARGE: "TooLarge"}
RESET_NONE, RESET_KEY_LEVEL, RESET_TWO_LEGS, RESET_NEW_EXTREME = 0, 1, 2, 3
RESET_REASON_NAMES = {RESET_NONE: "None", RESET_KEY_LEVEL: "KeyLevel", RESET_TWO_LEGS: "TwoLegs",
                      RESET_NEW_EXTREME: "NewExtreme"}

NAN = float("nan")


# --------------------------------------------------------------------------- shared building blocks
@dataclass(frozen=True)
class PaBarSnapshot:
    """port of struct PaBarSnapshot, lines 94-106 (a value type: never changed after creation)."""
    Index: int = 0
    Open: float = 0.0
    High: float = 0.0
    Low: float = 0.0
    Close: float = 0.0
    PrevHigh: float = 0.0
    PrevLow: float = 0.0
    HasPrev: bool = False
    Ema: float = 0.0


@dataclass
class SignalBarSettings:
    """port of class SignalBarSettings, lines 108-116."""
    MinBodyPct: float
    MinClosePct: float
    MaxBreakoutWickPct: float
    UseRejectionException: bool
    RejectionMinClosePct: float
    RejectionMaxWickPct: float


@dataclass
class SignalBarResult:
    """port of struct SignalBarResult, lines 118-126."""
    Ok: bool = False
    IsRejectionBar: bool = False
    BodyPct: float = 0.0
    ClosePct: float = 0.0
    WickPct: float = 0.0
    Reason: str | None = None


class SignalBarQuality:
    """port of static class SignalBarQuality, lines 128-183 (Rule 5)."""
    Eps = 1e-6

    @staticmethod
    def evaluate(is_long: bool, open_: float, high: float, low: float, close: float,
                 s: SignalBarSettings) -> SignalBarResult:
        # port of SignalBarQuality.Evaluate, lines 133-182
        eps = SignalBarQuality.Eps
        r = SignalBarResult()
        rng = high - low
        if rng <= 0:
            r.BodyPct = NAN
            r.ClosePct = NAN
            r.WickPct = NAN
            r.Reason = "zero range"
            return r

        r.BodyPct = abs(close - open_) / rng * 100.0
        r.ClosePct = (close - low if is_long else high - close) / rng * 100.0
        r.WickPct = (high - close if is_long else close - low) / rng * 100.0

        direction_ok = close > open_ if is_long else close < open_
        body_ok = r.BodyPct >= s.MinBodyPct - eps
        close_ok = r.ClosePct >= s.MinClosePct - eps
        wick_ok = r.WickPct <= s.MaxBreakoutWickPct + eps

        if direction_ok and body_ok and close_ok and wick_ok:
            r.Ok = True
            r.Reason = "ok"
            return r

        # ASSUMPTION: the rejection exception ignores the body completely, including its direction.
        if (s.UseRejectionException
                and r.ClosePct >= s.RejectionMinClosePct - eps
                and r.WickPct <= s.RejectionMaxWickPct + eps):
            r.Ok = True
            r.IsRejectionBar = True
            r.Reason = "ok (rejection bar)"
            return r

        if not direction_ok:
            r.Reason = "bar not bullish" if is_long else "bar not bearish"
        elif not body_ok:
            r.Reason = f"body {net_custom(r.BodyPct, 0, 0, '.')}% < {net_custom(s.MinBodyPct, 0, 0, '.')}%"
        elif not close_ok:
            r.Reason = f"close {net_custom(r.ClosePct, 0, 0, '.')}% < {net_custom(s.MinClosePct, 0, 0, '.')}%"
        else:
            r.Reason = f"wick {net_custom(r.WickPct, 0, 0, '.')}% > {net_custom(s.MaxBreakoutWickPct, 0, 0, '.')}%"
        return r


@dataclass
class RiskSettings:
    """port of class RiskSettings, lines 185-191."""
    StopMode: str
    RiskTemplateTicks: int
    RiskToleranceTicks: int
    MaxPullbackPct: float


@dataclass
class RiskResult:
    """port of struct RiskResult, lines 193-200."""
    Trigger: float = 0.0
    Stop: float = 0.0
    LimitPrice: float = 0.0
    RiskTicks: int = 0
    RiskClass: int = RISK_NONE


def _net_round(x: float) -> int:
    """Math.Round(double): banker's rounding (MidpointRounding.ToEven), like Python's round()."""
    return int(round(x))


class StopRiskCalculator:
    """port of static class StopRiskCalculator, lines 202-258."""

    @staticmethod
    def compute(is_long: bool, sig: PaBarSnapshot, tick_size: float, s: RiskSettings) -> RiskResult:
        # port of StopRiskCalculator.Compute, lines 205-247
        r = RiskResult()
        use_prev = s.StopMode == "SignalAndPrevBar" and sig.HasPrev

        if is_long:
            struct_low = min(sig.Low, sig.PrevLow) if use_prev else sig.Low
            r.Trigger = sig.High + tick_size
            r.Stop = struct_low - tick_size
            structure_ticks = (sig.High - struct_low) / tick_size
        else:
            struct_high = max(sig.High, sig.PrevHigh) if use_prev else sig.High
            r.Trigger = sig.Low - tick_size
            r.Stop = struct_high + tick_size
            structure_ticks = (struct_high - sig.Low) / tick_size

        r.RiskTicks = _net_round(abs(r.Trigger - r.Stop) / tick_size)
        max_risk = max(1, s.RiskTemplateTicks - 1)

        if r.RiskTicks <= max_risk + s.RiskToleranceTicks:
            r.RiskClass = RISK_DIRECT
        else:
            needed_ticks = float(r.RiskTicks - max_risk)
            if structure_ticks > 0 and needed_ticks <= s.MaxPullbackPct / 100.0 * structure_ticks + 1e-9:
                r.RiskClass = RISK_PULLBACK_LIMIT
                r.LimitPrice = r.Stop + max_risk * tick_size if is_long else r.Stop - max_risk * tick_size
            else:
                r.RiskClass = RISK_TOO_LARGE
        return r

    @staticmethod
    def class_suffix(c: int) -> str:
        # port of StopRiskCalculator.ClassSuffix, lines 249-257
        if c == RISK_PULLBACK_LIMIT:
            return " L"
        if c == RISK_TOO_LARGE:
            return " X"
        return ""


class PaLabelLayout:
    """port of static class PaLabelLayout, lines 260-276 (pixel rows around a bar)."""

    @staticmethod
    def row_height(font_size: int) -> int:
        # port of PaLabelLayout.RowHeight, lines 267-270
        return int(math.ceil(font_size * 1.7))

    @staticmethod
    def offset(below_low: bool, row: int, row_height: int, below_clearance: int) -> int:
        # port of PaLabelLayout.Offset, lines 272-275
        return -(below_clearance + row * row_height) if below_low else (row + 1) * row_height


class DayBarNumbering:
    """port of class DayBarNumbering, lines 278-308 (bar numbers restart on every new calendar date)."""

    def __init__(self):
        self.numbers: list[int] = []

    def update(self, current_bar: int, time0: datetime, time1: datetime) -> int:
        # port of DayBarNumbering.Update, lines 286-301
        if current_bar == 0:
            number = 1
        elif time0.date() != time1.date():
            number = 1
        else:
            number = self.get(current_bar - 1) + 1
        if current_bar < len(self.numbers):
            self.numbers[current_bar] = number
        else:
            self.numbers.append(number)
        return number

    def get(self, bar_index: int) -> int:
        # port of DayBarNumbering.Get, lines 304-307
        return self.numbers[bar_index] if 0 <= bar_index < len(self.numbers) else 0


@dataclass
class PaBarContext:
    """port of struct PaBarContext, lines 310-334."""
    BarNo: int = 0
    BarEnd: datetime | None = None
    BarStart: datetime | None = None
    Open: float = 0.0
    High: float = 0.0
    Low: float = 0.0
    Close: float = 0.0
    Ema: float = 0.0
    BarType: str | None = None
    Trend: int = 0
    KeyLevel: bool = False
    LongCount: int = 0
    LongInLeg: bool = False
    LongCandidateBarNo: int = 0
    LongOriginBarNo: int = 0
    LongStateText: str | None = None
    ShortCount: int = 0
    ShortInLeg: bool = False
    ShortCandidateBarNo: int = 0
    ShortOriginBarNo: int = 0
    ShortStateText: str | None = None


_INVALID_FILE_CHARS = '<>:"/\\|?*' + "".join(chr(i) for i in range(32))


class PaDiagnosticsLog:
    """port of class PaDiagnosticsLog, lines 336-531 (one CSV per trading day, ';', de-DE decimal comma).

    The file goes to `out_dir` instead of NinjaTrader.Core.Globals.UserDataDir, with `suffix` before ".csv"
    (EntryCount_ES_DEC26_20261009_CT.csv), so it never overwrites an NT file."""
    Header = ("BarNo;BarEndTime;BarStartTime;Indicator;Event;Direction;Count;Open;High;Low;Close;BarType;Trend;"
              "KeyLevel;LongCount;LongState;LongOriginBarNo;ShortCount;ShortState;ShortOriginBarNo;SignalBarNo;"
              "SignalH;SignalL;SignalO;SignalC;EMA;DistEmaTicks;BodyPct;ClosePct;WickPct;QualityOk;Trigger;Stop;"
              "RiskTicks;RiskClass;Reason")

    def __init__(self, indicator_name: str, instrument_name: str | None, out_dir: str | Path = ".",
                 suffix: str = ""):
        # port of the PaDiagnosticsLog constructor, lines 357-365
        self.Context = PaBarContext()
        self.indicator_name = indicator_name
        name = instrument_name or "Unknown"
        for c in _INVALID_FILE_CHARS:
            name = name.replace(c, "_")
        self.instrument_name = name.replace(" ", "_")
        self.out_dir = Path(out_dir)
        self.suffix = suffix
        self.writer = None
        self.file_day: date | None = None
        self.LastError: str | None = None
        self.paths: list[Path] = []          # files written (for the caller's summary)
        self._sb: list[str] = []

    def format(self, evt: str, direction: str, count: int, signal_bar_no: int,
               sig_high: float, sig_low: float, sig_open: float, sig_close: float, ema: float, dist_ema_ticks: float,
               body_pct: float, close_pct: float, wick_pct: float, quality: int, trigger: float, stop: float,
               risk_ticks: int, risk_class: int, reason: str | None) -> str:
        # port of PaDiagnosticsLog.Format, lines 371-397
        self._append_prefix(evt, direction, count, False)
        self._append_int(signal_bar_no)
        self._append_number(sig_high, 2, 4)
        self._append_number(sig_low, 2, 4)
        self._append_number(sig_open, 2, 4)
        self._append_number(sig_close, 2, 4)
        self._append_number(ema, 2, 4)
        self._append_number(dist_ema_ticks, 0, 1)
        self._append_number(body_pct, 0, 0)
        self._append_number(close_pct, 0, 0)
        self._append_number(wick_pct, 0, 0)
        sb = self._sb
        if quality >= 0:
            sb.append("true" if quality == 1 else "false")
        sb.append(";")
        self._append_number(trigger, 2, 4)
        self._append_number(stop, 2, 4)
        if risk_ticks > 0:
            sb.append(str(risk_ticks))
        sb.append(";")
        if risk_class != RISK_NONE:
            sb.append(RISK_CLASS_NAMES[risk_class])
        sb.append(";")
        if reason:
            sb.append(reason.replace(";", ","))
        return "".join(sb)

    def format_bar(self) -> str:
        # port of PaDiagnosticsLog.FormatBar, lines 400-408
        self._append_prefix("Bar", "", 0, True)
        sb = self._sb
        sb.append(";")                      # SignalBarNo
        sb.append(";;;;")                   # SignalH, SignalL, SignalO, SignalC
        self._append_number(self.Context.Ema, 2, 4)
        sb.append(";;;;;;;;;")              # DistEmaTicks .. RiskClass
        return "".join(sb)

    def _append_prefix(self, evt: str, direction: str, count: int, with_counts: bool) -> None:
        # port of PaDiagnosticsLog.AppendPrefix, lines 410-441
        self._sb = sb = []
        ctx = self.Context
        self._append_int(ctx.BarNo)
        sb.append(net_datetime(ctx.BarEnd)); sb.append(";")
        sb.append(net_datetime(ctx.BarStart)); sb.append(";")
        sb.append(self.indicator_name); sb.append(";")
        sb.append(evt); sb.append(";")
        sb.append(direction); sb.append(";")
        if count > 0:
            sb.append(str(count))
        sb.append(";")
        self._append_number(ctx.Open, 2, 4)
        self._append_number(ctx.High, 2, 4)
        self._append_number(ctx.Low, 2, 4)
        self._append_number(ctx.Close, 2, 4)
        sb.append(ctx.BarType or ""); sb.append(";")
        sb.append(str(ctx.Trend)); sb.append(";")
        sb.append("true" if ctx.KeyLevel else "false"); sb.append(";")
        if with_counts:
            sb.append(str(ctx.LongCount)); sb.append(";")
            self._append_state(ctx.LongStateText, ctx.LongInLeg, ctx.LongCandidateBarNo)
            self._append_int(ctx.LongOriginBarNo)
            sb.append(str(ctx.ShortCount)); sb.append(";")
            self._append_state(ctx.ShortStateText, ctx.ShortInLeg, ctx.ShortCandidateBarNo)
            self._append_int(ctx.ShortOriginBarNo)
        else:
            sb.append(";;;;;;")

    def _append_state(self, text: str | None, in_leg: bool, candidate_bar_no: int) -> None:
        # port of PaDiagnosticsLog.AppendState, lines 443-452
        sb = self._sb
        if text is not None:
            sb.append(text)
        elif in_leg:
            sb.append(f"Leg(signal bar {candidate_bar_no})")
        else:
            sb.append("Pullback")
        sb.append(";")

    def _append_int(self, value: int) -> None:
        # port of PaDiagnosticsLog.AppendInt, lines 454-459
        if value > 0:
            self._sb.append(str(value))
        self._sb.append(";")

    def _append_number(self, value: float, min_dec: int, max_dec: int) -> None:
        # port of PaDiagnosticsLog.AppendNumber, lines 461-466 ("0.00##" = 2..4, "0.#" = 0..1, "0" = 0..0)
        if value == value and value not in (float("inf"), float("-inf")):
            self._sb.append(net_custom(value, min_dec, max_dec, ","))
        self._sb.append(";")

    def write_csv(self, trading_day: date, line: str) -> None:
        # port of PaDiagnosticsLog.WriteCsv, lines 468-476
        if trading_day != self.file_day:
            self._open(trading_day)
        if self.writer is None:
            return
        self.writer.write(line + "\r\n")

    def _open(self, day: date) -> None:
        # port of PaDiagnosticsLog.Open, lines 478-505 (FileMode.Create: overwritten on every run)
        self.close()
        self.file_day = day
        self.out_dir.mkdir(parents=True, exist_ok=True)
        base = f"{self.indicator_name}_{self.instrument_name}_{day:%Y%m%d}"
        for attempt in range(5):
            path = self.out_dir / f"{base}{'' if attempt == 0 else '_' + str(attempt)}{self.suffix}.csv"
            try:
                self.writer = open(path, "w", encoding="utf-8-sig", newline="", buffering=65536)
                self.writer.write(self.Header + "\r\n")
                self.LastError = None
                self.paths.append(path)
                break
            except PermissionError as e:       # file open in Excel: try a suffixed name
                self.LastError = str(e)
            except OSError as e:
                self.LastError = str(e)
                break

    def flush(self) -> None:
        # port of PaDiagnosticsLog.Flush, lines 507-514
        if self.writer is not None:
            self.writer.flush()

    def close(self) -> None:
        # port of PaDiagnosticsLog.Close, lines 516-530
        if self.writer is None:
            return
        try:
            self.writer.flush()
            self.writer.close()
        except OSError:
            pass
        self.writer = None


# --------------------------------------------------------------------------- EntryCount
class SwingRing:
    """port of EntryCount.SwingRing, lines 539-561 (fixed-capacity ring of confirmed swing points)."""
    Capacity = 32

    def __init__(self):
        self.prices = [0.0] * self.Capacity
        self.bars = [0] * self.Capacity
        self.head = 0
        self.Count = 0

    def add(self, price: float, bar: int) -> None:
        self.head = (self.head + 1) % self.Capacity
        self.prices[self.head] = price
        self.bars[self.head] = bar
        if self.Count < self.Capacity:
            self.Count += 1

    def price(self, i: int) -> float:
        """i = 0 is the newest swing."""
        return self.prices[(self.head - i + self.Capacity) % self.Capacity]

    def bar(self, i: int) -> int:
        return self.bars[(self.head - i + self.Capacity) % self.Capacity]

    def clear(self) -> None:
        self.Count = 0
        self.head = 0


class CountState:
    """port of EntryCount.CountState, lines 563-585 (counting state machine of one direction)."""

    def __init__(self, is_long: bool):
        self.IsLong = is_long
        self.Count = 0
        self.InLeg = False
        self.OriginPrice = 0.0
        self.OriginBar = 0
        self.Candidate = PaBarSnapshot()
        self.LegExtreme = 0.0
        self.PrevLegExtreme = 0.0
        self.HasPrevLeg = False
        self.F2Armed = False
        self.F2Level = 0.0
        self.F2ArmBar = 0
        self.F2RunExtreme = 0.0
        self.F2RunExtremeBar = 0
        self.Paused = False                # tuning EC-11: counter-trend count paused after its F2E
        self.F2OriginPrice = 0.0           # tuning EC-12: origin of the count the 2E came from


BAR_TYPE_FIRST = "First"
BAR_TYPE_INSIDE = "Inside"
BAR_TYPE_UP = "Up"
BAR_TYPE_DOWN = "Down"
BAR_TYPE_NO_BREAK = "NoBreak"
BAR_TYPE_OUTSIDE_UP = "OutsideUp"
BAR_TYPE_OUTSIDE_DOWN = "OutsideDown"
BAR_TYPE_OUTSIDE_BOTH = "OutsideBoth"
BAR_TYPE_OUTSIDE_IGNORED = "OutsideIgnored"


class EntryCount:
    """port of class EntryCount, lines 535-1884."""
    Name = "EntryCount"

    def __init__(self, cfg: EntryCountConfig | None = None, tick_size: float = 0.25,
                 instrument_name: str | None = None, csv_dir: str | Path = ".", csv_suffix: str = "",
                 draw: Drawings | None = None, printer: Callable[[str], None] | None = None):
        # port of EntryCount.OnStateChange, State.DataLoaded, lines 748-815
        self.cfg = cfg = replace(cfg) if cfg is not None else EntryCountConfig()
        self.TickSize = tick_size
        self.draw = draw if draw is not None else Drawings()
        self.printer = printer or print

        # Bars and EMA(Close, EmaPeriod) of NinjaTrader (EMA: first value = first close)
        self.bars: list[PaBar] = []
        self.ema_values: list[float] = []
        self._ema_c1 = 2.0 / (1 + cfg.EmaPeriod)
        self._ema_c2 = 1 - 2.0 / (1 + cfg.EmaPeriod)
        self.CurrentBar = -1

        self.longState = CountState(True)
        self.shortState = CountState(False)
        self.swingHighs = SwingRing()
        self.swingLows = SwingRing()
        self.barNumbering = DayBarNumbering()

        self.signalSettings = SignalBarSettings(cfg.MinBodyPct, cfg.MinClosePct, cfg.MaxBreakoutWickPct,
                                                cfg.UseRejectionException, cfg.RejectionMinClosePct,
                                                cfg.RejectionMaxWickPct)
        self.riskSettings = RiskSettings(cfg.StopMode, cfg.RiskTemplateTicks, cfg.RiskToleranceTicks,
                                         cfg.MaxPullbackPct)

        # Series<T>[0] of the current bar (carried forward like CarryForwardSeries); emaTouch keeps its history
        self.longCount = self.shortCount = 0
        self.longEntryEvent = self.shortEntryEvent = 0
        self.failedSecondLong = self.failedSecondShort = False
        self.longSignalHigh = self.longSignalLow = self.shortSignalHigh = self.shortSignalLow = 0.0
        self.longSignalBar = self.shortSignalBar = -1
        self.longSignalQualityOk = self.shortSignalQualityOk = False
        self.longStop = self.shortStop = 0.0
        self.longRiskTicks = self.shortRiskTicks = 0
        self.longRiskClass = self.shortRiskClass = 0
        self.longResetReason = self.shortResetReason = 0
        self.emaTouch: list[bool] = []
        self.trendState = 0
        self.lastSecondLongBar = self.lastSecondShortBar = -1
        self.longEntryOriginPrice = self.shortEntryOriginPrice = 0.0
        self.longEntryOriginBar = self.shortEntryOriginBar = -1

        self.halfTick = tick_size * 0.5
        self.rowHeight = PaLabelLayout.row_height(cfg.LabelFontSize)
        self.labelFontSize = cfg.LabelFontSize
        self.smallFontSize = max(7, cfg.LabelFontSize - 2)

        self.diag: PaDiagnosticsLog | None = None
        if cfg.DebugLog or cfg.ExportCsv:
            self.diag = PaDiagnosticsLog("EntryCount", instrument_name, csv_dir, csv_suffix)
        self.tradingDay: date | None = None

        self.refHigh = self.refLow = 0.0
        self.currentSnap = PaBarSnapshot()
        self.currentBarNo = 0
        self.currentUp = self.currentDown = False
        self.currentOutsideUpOnly = self.currentOutsideDownOnly = False
        self.currentTrend = 0

        # Intrabar order from the 1-tick series
        self.tickTrackReady = False
        self.tickRefHigh = self.tickRefLow = 0.0
        self.tickSeq = 0
        self.tickFirstUp = self.tickFirstDown = 0
        self.tickCountInBar = 0
        self.currentHighFirst = False
        self.currentOrderSource = ""

    def terminate(self) -> None:
        # port of EntryCount.OnStateChange, State.Terminated, lines 821-828
        if self.diag is not None:
            self.diag.close()

    # --- NinjaScript series access (barsAgo) ------------------------------------------------
    def _bar(self, ago: int) -> PaBar:
        return self.bars[self.CurrentBar - ago]

    def ema(self, ago: int) -> float:
        return self.ema_values[self.CurrentBar - ago]

    def _update_ema(self, close: float) -> None:
        # NinjaTrader @EMA.cs: Value[0] = CurrentBar == 0 ? Input[0] : Input[0] * constant1 + constant2 * Value[1]
        if not self.ema_values:
            self.ema_values.append(close)
        else:
            self.ema_values.append(close * self._ema_c1 + self._ema_c2 * self.ema_values[-1])

    # --- OnBarUpdate ----------------------------------------------------------------------
    def on_tick(self, price: float) -> None:
        """OnBarUpdate with BarsInProgress == TickSeries (lines 838-842)."""
        self.track_intrabar_tick(price)

    def on_bar_update(self, bar: PaBar) -> None:
        # port of EntryCount.OnBarUpdate (primary series), lines 836-961
        self.bars.append(bar)
        self.CurrentBar = cb = len(self.bars) - 1
        self._update_ema(bar.close)
        cfg = self.cfg
        b0 = bar
        b1 = self.bars[cb - 1] if cb > 0 else bar

        self.resolve_intrabar_order()

        # sessionIterator (ExportCsv only): the trading day names the CSV file
        self.tradingDay = trading_day_of(b0.time)

        ema0 = self.ema(0)
        self.currentTrend = self.compute_trend(ema0)
        self.trendState = self.currentTrend
        self.currentBarNo = self.barNumbering.update(cb, b0.time, b1.time if cb > 0 else b0.time)

        self.detect_swings()
        key_contact = self.compute_key_level_contact(ema0, self.currentTrend)
        self.emaTouch.append(key_contact)

        self.carry_forward_series()

        self.currentSnap = self.snapshot(ema0)
        self.begin_diagnostics_bar(ema0, key_contact)

        if cb == 0:
            self.init_state(self.longState)
            self.init_state(self.shortState)
            self.refHigh = b0.high
            self.refLow = b0.low
            self.write_count_series()
            self.end_diagnostics_bar(BAR_TYPE_FIRST)
            return

        # Bar classification against the DIRECT previous bar (two inside bars: the second is measured at the first).
        half = self.halfTick
        inside = b0.high <= b1.high + half and b0.low >= b1.low - half
        processed = False
        bar_type = BAR_TYPE_INSIDE
        self.currentUp = False
        self.currentDown = False
        self.currentOutsideUpOnly = False
        self.currentOutsideDownOnly = False

        if not inside:
            # Leg breaks are measured against the last non-inside bar (inside bars are skipped in leg detection).
            raw_up = b0.high > self.refHigh + half
            raw_down = b0.low < self.refLow - half
            processed = True
            bar_type = BAR_TYPE_UP if raw_up else BAR_TYPE_DOWN if raw_down else BAR_TYPE_NO_BREAK

            if raw_up and raw_down:
                # Outside bars: OutsideBarPolicy decides which side counts (default BothSides).
                if cfg.OutsideBarPolicy == "CloseDirection":
                    up_side = b0.close > b0.open or (b0.close == b0.open and b0.close >= (b0.high + b0.low) * 0.5)
                    raw_up = up_side
                    raw_down = not up_side
                    bar_type = BAR_TYPE_OUTSIDE_UP if up_side else BAR_TYPE_OUTSIDE_DOWN
                    self.currentOutsideUpOnly = up_side
                    self.currentOutsideDownOnly = not up_side
                elif cfg.OutsideBarPolicy == "BothSides":
                    bar_type = BAR_TYPE_OUTSIDE_BOTH
                elif cfg.OutsideBarPolicy == "Ignore":
                    processed = False
                    bar_type = BAR_TYPE_OUTSIDE_IGNORED

            if processed:
                self.currentUp = raw_up
                self.currentDown = raw_down
        if self.diag is not None:
            self.diag.Context.BarType = bar_type

        self.update_f2_run_extreme(self.longState)
        self.update_f2_run_extreme(self.shortState)

        self.end_pause(self.longState)
        self.end_pause(self.shortState)

        self.apply_key_level_reset(self.longState, key_contact)
        self.apply_key_level_reset(self.shortState, key_contact)

        if processed:
            self.run_machine(self.longState)
            self.run_machine(self.shortState)
            self.refHigh = b0.high
            self.refLow = b0.low
        elif inside and cfg.InsideBarAsSignal:
            self.take_inside_bar_as_candidate(self.longState)
            self.take_inside_bar_as_candidate(self.shortState)

        if not inside:
            self.check_failed_second(self.longState)
            self.check_failed_second(self.shortState)

        self.apply_extreme_and_key_level_resets(self.longState, key_contact)
        self.apply_extreme_and_key_level_resets(self.shortState, key_contact)

        self.write_count_series()
        self.end_diagnostics_bar(bar_type)

    # --- Intrabar order (lines 963-1002) ----------------------------------------------------
    def track_intrabar_tick(self, price: float) -> None:
        # port of EntryCount.TrackIntrabarTick, lines 966-977
        if not self.tickTrackReady:
            return
        self.tickSeq += 1
        self.tickCountInBar += 1
        if self.tickFirstUp == 0 and price > self.tickRefHigh + self.halfTick:
            self.tickFirstUp = self.tickSeq
        if self.tickFirstDown == 0 and price < self.tickRefLow - self.halfTick:
            self.tickFirstDown = self.tickSeq

    def resolve_intrabar_order(self) -> None:
        # port of EntryCount.ResolveIntrabarOrder, lines 984-1000
        b0 = self._bar(0)
        self.currentHighFirst = b0.close < b0.open or (b0.close == b0.open and b0.high - b0.open <= b0.open - b0.low)
        self.currentOrderSource = "bar color"
        if (self.CurrentBar > 0 and self.tickTrackReady and self.tickCountInBar > 0
                and (self.tickFirstUp > 0 or self.tickFirstDown > 0)):
            self.currentHighFirst = self.tickFirstDown == 0 or (0 < self.tickFirstUp < self.tickFirstDown)
            self.currentOrderSource = "ticks"

        self.tickRefHigh = b0.high
        self.tickRefLow = b0.low
        self.tickFirstUp = 0
        self.tickFirstDown = 0
        self.tickCountInBar = 0
        self.tickTrackReady = True

    # --- Diagnostics bar context (lines 1004-1055) ------------------------------------------
    def begin_diagnostics_bar(self, ema0: float, key_contact: bool) -> None:
        # port of EntryCount.BeginDiagnosticsBar, lines 1016-1034
        if self.diag is None:
            return
        b0 = self._bar(0)
        self.diag.Context = PaBarContext(
            BarNo=self.currentBarNo,
            BarEnd=b0.time,
            BarStart=self._bar(1).time if self.CurrentBar > 0 else b0.time,
            Open=b0.open, High=b0.high, Low=b0.low, Close=b0.close,
            Ema=ema0,
            BarType=BAR_TYPE_FIRST,
            Trend=self.currentTrend,
            KeyLevel=key_contact)

    def end_diagnostics_bar(self, bar_type: str) -> None:
        # port of EntryCount.EndDiagnosticsBar, lines 1037-1053
        if self.diag is None:
            return
        ctx, bn = self.diag.Context, self.barNumbering
        ctx.BarType = bar_type
        ctx.LongCount = self.longState.Count
        ctx.LongInLeg = self.longState.InLeg
        ctx.LongCandidateBarNo = bn.get(self.longState.Candidate.Index)
        ctx.LongOriginBarNo = bn.get(self.longState.OriginBar)
        ctx.ShortCount = self.shortState.Count
        ctx.ShortInLeg = self.shortState.InLeg
        ctx.ShortCandidateBarNo = bn.get(self.shortState.Candidate.Index)
        ctx.ShortOriginBarNo = bn.get(self.shortState.OriginBar)
        if self.longState.Paused:
            ctx.LongStateText = "Paused"
        if self.shortState.Paused:
            ctx.ShortStateText = "Paused"

        if self.cfg.ExportCsv and self.cfg.CsvIncludeBars:
            self.diag.write_csv(self.tradingDay, self.diag.format_bar())

    # --- Counting (lines 1057-1360) -------------------------------------------------------
    def init_state(self, c: CountState) -> None:
        # port of EntryCount.InitState, lines 1059-1067
        b0 = self._bar(0)
        c.Count = 0
        c.InLeg = False
        c.OriginPrice = b0.high if c.IsLong else b0.low
        c.OriginBar = self.CurrentBar
        c.HasPrevLeg = False
        c.F2Armed = False
        self.pause_new_count_beyond_ema(c)    # tuning EC-13

    def leg_move(self, c: CountState) -> bool:
        # port of EntryCount.LegMove, line 1069
        return self.currentDown if c.IsLong else self.currentUp

    def start_leg(self, c: CountState) -> None:
        # port of EntryCount.StartLeg, lines 1071-1076
        b0 = self._bar(0)
        c.InLeg = True
        c.Candidate = self.currentSnap
        c.LegExtreme = b0.low if c.IsLong else b0.high

    def run_machine(self, c: CountState) -> None:
        # port of EntryCount.RunMachine, lines 1078-1112
        if c.Paused:                        # tuning EC-11
            return
        b0, half = self._bar(0), self.halfTick
        if not c.InLeg:
            # Pullback (or waiting at the origin): a bar breaking the reference bar in leg direction starts a new leg.
            if self.leg_move(c):
                self.start_leg(c)
            return

        # Trigger = break of the signal bar candidate by at least 1 tick.
        candidate_break = b0.high > c.Candidate.High + half if c.IsLong else b0.low < c.Candidate.Low - half
        blocked_by_outside_policy = self.currentOutsideDownOnly if c.IsLong else self.currentOutsideUpOnly
        if candidate_break and not blocked_by_outside_policy:
            leg_too_short = (self.cfg.RequireEqualOrHigherLeg and c.Count >= 1 and c.HasPrevLeg
                             and (c.LegExtreme > c.PrevLegExtreme + half if c.IsLong
                                  else c.LegExtreme < c.PrevLegExtreme - half))

            c.InLeg = False
            if leg_too_short:
                # Strict ABC: the leg did not reach the extreme of the prior leg, the pullback simply continues.
                self.log_event("Entry", c.IsLong, c.Count + 1, c.Candidate, True,
                               "skipped: leg did not reach prior leg extreme (RequireEqualOrHigherLeg)")
                return
            self.fire_entry(c)
            return

        # Leg continues: the latest bar is the new signal bar candidate.
        c.Candidate = self.currentSnap
        c.LegExtreme = min(c.LegExtreme, b0.low) if c.IsLong else max(c.LegExtreme, b0.high)

    def take_inside_bar_as_candidate(self, c: CountState) -> None:
        # port of EntryCount.TakeInsideBarAsCandidate, lines 1118-1122
        if c.InLeg and not c.Paused:        # tuning EC-11: no candidate while paused
            c.Candidate = self.currentSnap

    def fire_entry(self, c: CountState) -> None:
        # port of EntryCount.FireEntry, lines 1124-1188 (Alert not ported, realtime only)
        b0 = self._bar(0)
        c.Count += 1
        c.PrevLegExtreme = c.LegExtreme
        c.HasPrevLeg = True

        sig = c.Candidate
        q = SignalBarQuality.evaluate(c.IsLong, sig.Open, sig.High, sig.Low, sig.Close, self.signalSettings)
        risk = StopRiskCalculator.compute(c.IsLong, sig, self.TickSize, self.riskSettings)

        if c.IsLong:
            self.longEntryEvent = c.Count
            self.longSignalHigh = sig.High
            self.longSignalLow = sig.Low
            self.longSignalBar = sig.Index
            self.longSignalQualityOk = q.Ok
            self.longStop = risk.Stop
            self.longRiskTicks = risk.RiskTicks
            self.longRiskClass = risk.RiskClass
            self.longEntryOriginPrice = c.OriginPrice
            self.longEntryOriginBar = c.OriginBar
        else:
            self.shortEntryEvent = c.Count
            self.shortSignalHigh = sig.High
            self.shortSignalLow = sig.Low
            self.shortSignalBar = sig.Index
            self.shortSignalQualityOk = q.Ok
            self.shortStop = risk.Stop
            self.shortRiskTicks = risk.RiskTicks
            self.shortRiskClass = risk.RiskClass
            self.shortEntryOriginPrice = c.OriginPrice
            self.shortEntryOriginBar = c.OriginBar

        if c.Count == 2:
            c.F2Armed = True
            c.F2Level = sig.Low if c.IsLong else sig.High
            c.F2OriginPrice = c.OriginPrice     # tuning EC-12
            c.F2ArmBar = self.CurrentBar
            c.F2RunExtreme = b0.high if c.IsLong else b0.low
            c.F2RunExtremeBar = self.CurrentBar
            if c.IsLong:
                self.lastSecondLongBar = self.CurrentBar
            else:
                self.lastSecondShortBar = self.CurrentBar
        elif c.Count > 2:
            # A third entry ends the failed-second-entry watch of the preceding 2E.
            c.F2Armed = False

        self.draw_entry_label(c.IsLong, c.Count, sig, q.Ok, risk)

        if self.diag is not None:
            self.log_entry(c.IsLong, c.Count, sig, q, risk)

    def update_f2_run_extreme(self, c: CountState) -> None:
        # port of EntryCount.UpdateF2RunExtreme, lines 1190-1199
        if not c.F2Armed:
            return
        b0 = self._bar(0)
        if b0.high > c.F2RunExtreme if c.IsLong else b0.low < c.F2RunExtreme:
            c.F2RunExtreme = b0.high if c.IsLong else b0.low
            c.F2RunExtremeBar = self.CurrentBar

    def check_failed_second(self, c: CountState) -> None:
        # port of EntryCount.CheckFailedSecond, lines 1201-1254 (Alert not ported, realtime only)
        cb = self.CurrentBar
        if not c.F2Armed or cb < c.F2ArmBar:
            return
        b0, b1, half, cfg = self._bar(0), self._bar(1), self.halfTick, self.cfg
        lsl = "L" if c.IsLong else "S"

        # Same-bar F2E (user rule): only if the entry side was traded first (tick data, else bar color).
        trigger_bar = cb == c.F2ArmBar
        if trigger_bar and c.IsLong != self.currentHighFirst:
            if self.diag is not None and (b0.low < c.F2Level - half if c.IsLong else b0.high > c.F2Level + half):
                self.log_event("F2E", c.IsLong, 2, self.currentSnap, False,
                               f"no same-bar F2E: signal bar {'low' if c.IsLong else 'high'} {net_g(c.F2Level)} "
                               f"traded before the 2E trigger ({self.currentOrderSource})")
            return

        # PriorBarBreak: fails as soon as a later bar trades 1 tick beyond the DIRECT prior bar.
        # SignalBarBreak: only when the extreme of the 2E signal bar is traded.
        prior_bar_mode = cfg.FailedEntryMode == "PriorBarBreak" and not trigger_bar
        if prior_bar_mode:
            failed = b0.low < b1.low - half if c.IsLong else b0.high > b1.high + half
        else:
            failed = b0.low < c.F2Level - half if c.IsLong else b0.high > c.F2Level + half
        if not failed:
            return

        c.F2Armed = False
        if c.IsLong:
            self.failedSecondLong = True
        else:
            self.failedSecondShort = True

        if cfg.ShowLabels and cfg.ShowFailedEntries and (cfg.ShowLongCount if c.IsLong else cfg.ShowShortCount):
            self.draw.put(DrawText(("EC_F2L_" if c.IsLong else "EC_F2S_") + str(cb), "F", cb,
                                   b0.low if c.IsLong else b0.high, self.label_offset(c.IsLong, 1),
                                   cfg.FailedLongColor if c.IsLong else cfg.FailedShortColor,
                                   self.labelFontSize, True))

        if self.diag is not None:
            if prior_bar_mode:
                reason = (f"2E{lsl} failed: prior bar {'low' if c.IsLong else 'high'} "
                          f"{net_g(b1.low if c.IsLong else b1.high)} (bar {self.barNumbering.get(cb - 1)}) traded")
            else:
                on_trigger = f" on its trigger bar ({self.currentOrderSource})" if trigger_bar else ""
                reason = (f"2E{lsl} failed{on_trigger}: signal bar {'low' if c.IsLong else 'high'} "
                          f"{net_g(c.F2Level)} traded")
            self.log_event("F2E", c.IsLong, 2, self.currentSnap, False, reason)

        # Reset b): the two-legged pullback is complete. New origin = swing extreme formed by the 2E/F2E.
        # ASSUMPTION: "completed 2E/F2E" means the F2E event; a successful 2E keeps counting (3E...).
        if cfg.ResetAfterTwoLegs:
            self.reset_count(c, RESET_TWO_LEGS, c.F2RunExtreme, c.F2RunExtremeBar)

        self.start_pause(c)

    # --- Tuning EC-11 (CHANGELOG_Tuning.md Nr. 1, in EntryCount.cs since 13.10.2026) ---------------
    def start_pause(self, c: CountState) -> None:
        """After an F2E against the EMA trend (F2ES in an uptrend, F2EL in a downtrend) the count of that
        direction pauses until a bar closes on the other side of the EMA (end_pause).
        EC-13: also after an F2E that closes beyond the EMA (F2ES above it, F2EL below it)."""
        if c.Paused:
            return
        lsl = "L" if c.IsLong else "S"
        if self.cfg.PauseCountAfterCounterTrendF2E and self.currentTrend == (-1 if c.IsLong else 1):
            self.pause(c, f"F2E{lsl} against the {'downtrend' if c.IsLong else 'uptrend'}")
        elif self.cfg.CountOnlyFromEmaSide and self.beyond_ema(c):
            self.pause(c, f"F2E{lsl} with close {'below' if c.IsLong else 'above'} the EMA")

    def pause(self, c: CountState, why: str) -> None:
        c.Paused = True
        c.InLeg = False
        c.F2Armed = False
        if self.diag is not None:
            self.log_event("CountPaused", c.IsLong, 0, self.currentSnap, False,
                           f"{why}: {'long' if c.IsLong else 'short'} count paused until a close "
                           f"{'above' if c.IsLong else 'below'} the EMA")

    # --- Tuning EC-13 (CHANGELOG_Tuning.md Nr. 7, in EntryCount.cs since 13.10.2026) ---------------
    def beyond_ema(self, c: CountState) -> bool:
        """Current bar closes on the wrong side of the EMA for this direction (short: above, long: below)."""
        b0, ema0 = self._bar(0), self.ema(0)
        return b0.close < ema0 if c.IsLong else b0.close > ema0

    def pause_new_count_beyond_ema(self, c: CountState) -> None:
        """A count only starts on its side of the EMA: a count that would start from 0 on a bar closing beyond
        the EMA (first bar, reset d/a/c) pauses until a close back on its side (end_pause). A running count is
        not affected; it goes on through the EMA to its F2E (start_pause)."""
        if not self.cfg.CountOnlyFromEmaSide or c.Paused or not self.beyond_ema(c):
            return
        self.pause(c, f"count would start with close {'below' if c.IsLong else 'above'} the EMA")

    def end_pause(self, c: CountState) -> None:
        """First close beyond the EMA ends the pause: the count restarts like on the first bar (origin = this bar)."""
        if not c.Paused:
            return
        b0, ema0 = self._bar(0), self.ema(0)
        if not (b0.close > ema0 if c.IsLong else b0.close < ema0):
            return
        c.Paused = False
        c.Count = 0
        c.InLeg = False
        c.HasPrevLeg = False
        c.OriginPrice = b0.high if c.IsLong else b0.low
        c.OriginBar = self.CurrentBar
        if self.diag is not None:
            self.log_event("CountResumed", c.IsLong, 0, self.currentSnap, False,
                           f"close {net_g(b0.close)} {'above' if c.IsLong else 'below'} EMA {net_g(round(ema0, 4))}; "
                           f"new origin {net_g(c.OriginPrice)} @bar {self.currentBarNo}")

    def apply_extreme_and_key_level_resets(self, c: CountState, key_contact: bool) -> None:
        # port of EntryCount.ApplyExtremeAndKeyLevelResets, lines 1256-1282
        if c.Paused:                        # tuning EC-11: no resets while paused
            return
        b0, half, cfg = self._bar(0), self.halfTick, self.cfg
        # Reset d) ASSUMPTION: a new extreme beyond the origin moves the origin (the 2E, if any, has succeeded).
        beyond = b0.high > c.OriginPrice + half if c.IsLong else b0.low < c.OriginPrice - half
        # tuning EC-14: a bar reaching the origin exactly (double top/bottom) resets the count as well
        equal = (cfg.ResetOnEqualExtreme and not beyond and c.OriginBar != self.CurrentBar
                 and abs((b0.high if c.IsLong else b0.low) - c.OriginPrice) < half)
        if cfg.ResetOnNewExtreme and (beyond or equal):
            # tuning EC-12: the F2E watch only ends beyond the origin of the 2E's count, not beyond an origin
            # that a key level reset moved after the 2E (EC-14: an equal extreme never ends it)
            if beyond and (not cfg.F2EWatchUntil2EOrigin or (b0.high > c.F2OriginPrice + half if c.IsLong
                                                             else b0.low < c.F2OriginPrice - half)):
                c.F2Armed = False
            self.reset_count(c, RESET_NEW_EXTREME, b0.high if c.IsLong else b0.low, self.CurrentBar)

        # Reset a)/c) ASSUMPTION: CounterTrendOnly resets only the count against the EMA trend.
        if not cfg.ResetOnKeyLevel or not key_contact:
            return

        applies = cfg.ResetScope == "Both" or (self.currentTrend == -1 if c.IsLong else self.currentTrend == 1)
        if not applies:
            return

        # The reset on a new contact already ran before the count (apply_key_level_reset).
        if c.Count == 0 and not c.InLeg and (b0.high > c.OriginPrice if c.IsLong else b0.low < c.OriginPrice):
            # Still waiting at the key level: just extend the origin to the extreme of the touch.
            c.OriginPrice = b0.high if c.IsLong else b0.low
            c.OriginBar = self.CurrentBar

    def apply_key_level_reset(self, c: CountState, key_contact: bool) -> None:
        # port of EntryCount.ApplyKeyLevelReset, lines 1289-1306
        cfg = self.cfg
        if not cfg.ResetOnKeyLevel or not key_contact or c.Paused:     # tuning EC-11: not while paused
            return

        applies = cfg.ResetScope == "Both" or (self.currentTrend == -1 if c.IsLong else self.currentTrend == 1)
        if not applies:
            return

        # Only a NEW contact resets (prior bar without key level contact).
        new_contact = self.CurrentBar == 0 or not self.emaTouch[self.CurrentBar - 1]
        if new_contact and (c.Count > 0 or c.InLeg):
            b0 = self._bar(0)
            self.reset_count(c, RESET_KEY_LEVEL, b0.high if c.IsLong else b0.low, self.CurrentBar, True)

    def reset_count(self, c: CountState, reason: int, origin_price: float, origin_bar: int,
                    keep_leg: bool = False) -> None:
        # port of EntryCount.ResetCount, lines 1308-1347
        cfg = self.cfg
        meaningful = c.Count > 0 or reason == RESET_TWO_LEGS
        count_before = c.Count

        c.Count = 0
        c.OriginPrice = origin_price
        c.OriginBar = origin_bar
        c.HasPrevLeg = False

        # The bar that caused the reset may already be the first bar of the new leg.
        if keep_leg:
            pass        # reset before counting: the running leg (and its signal bar) stays
        elif self.leg_move(c):
            self.start_leg(c)
        else:
            c.InLeg = False

        if not meaningful:
            if reason != RESET_TWO_LEGS:
                self.pause_new_count_beyond_ema(c)    # tuning EC-13 (after an F2E: start_pause)
            return

        if c.IsLong:
            if self.longResetReason == 0:
                self.longResetReason = reason
        elif self.shortResetReason == 0:
            self.shortResetReason = reason

        if cfg.ShowLabels and cfg.ShowCountOrigin and (cfg.ShowLongCount if c.IsLong else cfg.ShowShortCount):
            bars_ago = max(0, self.CurrentBar - origin_bar)
            tag = ("EC_OL_" if c.IsLong else "EC_OS_") + str(self.CurrentBar)
            self.draw.put(DrawDot(tag, self.CurrentBar - bars_ago, origin_price, cfg.OriginColor))
            self.draw.put(DrawText(tag + "_T", self.reset_short_text(reason), self.CurrentBar - bars_ago,
                                   origin_price, self.label_offset(not c.IsLong, 2), cfg.OriginColor,
                                   self.smallFontSize, False))

        if self.diag is not None:
            self.log_event("Reset", c.IsLong, count_before, self.currentSnap, False,
                           f"{RESET_REASON_NAMES[reason]}; new origin {net_g(origin_price)} "
                           f"@bar {self.barNumbering.get(origin_bar)}")
        if reason != RESET_TWO_LEGS:
            self.pause_new_count_beyond_ema(c)    # tuning EC-13 (after an F2E: start_pause)

    @staticmethod
    def reset_short_text(reason: int) -> str:
        # port of EntryCount.ResetShortText, lines 1349-1358
        return {RESET_KEY_LEVEL: "R:KL", RESET_TWO_LEGS: "R:2L", RESET_NEW_EXTREME: "R:NX"}.get(reason, "R")

    # --- Trend, swings, key levels (lines 1362-1444) ------------------------------------------
    def compute_trend(self, ema0: float) -> int:
        # port of EntryCount.ComputeTrend, lines 1364-1374
        cfg = self.cfg
        if self.CurrentBar < cfg.EmaSlopeLookback:
            return 0
        slope_ticks = (ema0 - self.ema(cfg.EmaSlopeLookback)) / self.TickSize
        if slope_ticks >= cfg.MinEmaSlopeTicks - 1e-9:
            return 1
        if slope_ticks <= -cfg.MinEmaSlopeTicks + 1e-9:
            return -1
        return 0

    def detect_swings(self) -> None:
        # port of EntryCount.DetectSwings, lines 1376-1396
        n = self.cfg.SwingStrength
        if self.CurrentBar < 2 * n:
            return
        is_high = True
        is_low = True
        h = self._bar(n).high
        l = self._bar(n).low
        k = 1
        while k <= n and (is_high or is_low):
            # Left side strict, right side allows equal values (the first of equal extremes is the swing).
            if not (h > self._bar(n + k).high and h >= self._bar(n - k).high):
                is_high = False
            if not (l < self._bar(n + k).low and l <= self._bar(n - k).low):
                is_low = False
            k += 1
        if is_high:
            self.swingHighs.add(h, self.CurrentBar - n)
        if is_low:
            self.swingLows.add(l, self.CurrentBar - n)

    def compute_key_level_contact(self, ema0: float, trend: int) -> bool:
        # port of EntryCount.ComputeKeyLevelContact, lines 1398-1442
        cfg, b0 = self.cfg, self._bar(0)
        tol = cfg.KeyLevelToleranceTicks * self.TickSize
        if b0.low - tol <= ema0 and b0.high + tol >= ema0:
            return True

        if cfg.UseSwingSR:
            sr_tol = cfg.SrToleranceTicks * self.TickSize
            for i in range(min(cfg.SrLookbackSwings, self.swingHighs.Count)):
                p = self.swingHighs.price(i)
                if b0.low - sr_tol <= p and b0.high + sr_tol >= p:
                    return True
            for i in range(min(cfg.SrLookbackSwings, self.swingLows.Count)):
                p = self.swingLows.price(i)
                if b0.low - sr_tol <= p and b0.high + sr_tol >= p:
                    return True

        # ASSUMPTION: trend line = line through the last two confirmed swings in trend direction
        # (uptrend: rising swing lows, downtrend: falling swing highs), projected to the current bar.
        if cfg.UseTrendlines and trend != 0:
            ring = self.swingLows if trend > 0 else self.swingHighs
            if ring.Count >= 2:
                p2, p1 = ring.price(0), ring.price(1)
                b2, b1 = ring.bar(0), ring.bar(1)
                valid = b2 > b1 and (p2 > p1 if trend > 0 else p2 < p1)
                if valid:
                    line = p1 + (p2 - p1) * (self.CurrentBar - b1) / float(b2 - b1)
                    if b0.low - tol <= line and b0.high + tol >= line:
                        return True
        return False

    # --- Series helpers (lines 1446-1525) --------------------------------------------------
    def snapshot(self, ema0: float) -> PaBarSnapshot:
        # port of EntryCount.Snapshot, lines 1448-1461
        b0 = self._bar(0)
        has_prev = self.CurrentBar > 0
        b1 = self._bar(1) if has_prev else b0
        return PaBarSnapshot(Index=self.CurrentBar, Open=b0.open, High=b0.high, Low=b0.low, Close=b0.close,
                             Ema=ema0, HasPrev=has_prev, PrevHigh=b1.high, PrevLow=b1.low)

    def carry_forward_series(self) -> None:
        # port of EntryCount.CarryForwardSeries, lines 1463-1517 (values carry over by staying in the attributes)
        self.longEntryEvent = 0
        self.shortEntryEvent = 0
        self.failedSecondLong = False
        self.failedSecondShort = False
        self.longResetReason = 0
        self.shortResetReason = 0

        if self.CurrentBar == 0:
            self.longSignalHigh = self.longSignalLow = self.shortSignalHigh = self.shortSignalLow = 0.0
            self.longSignalBar = self.shortSignalBar = -1
            self.longSignalQualityOk = self.shortSignalQualityOk = False
            self.longStop = self.shortStop = 0.0
            self.longRiskTicks = self.shortRiskTicks = 0
            self.longRiskClass = self.shortRiskClass = 0
            self.lastSecondLongBar = self.lastSecondShortBar = -1
            self.longEntryOriginPrice = self.shortEntryOriginPrice = 0.0
            self.longEntryOriginBar = self.shortEntryOriginBar = -1

    def write_count_series(self) -> None:
        # port of EntryCount.WriteCountSeries, lines 1519-1523
        self.longCount = self.longState.Count
        self.shortCount = self.shortState.Count

    # --- Drawing and diagnostics (lines 1527-1592) -------------------------------------------
    def label_offset(self, below_low: bool, row: int) -> int:
        # port of EntryCount.LabelOffset, lines 1529-1532
        return PaLabelLayout.offset(below_low, row, self.rowHeight, self.cfg.BarNumberClearance)

    def draw_entry_label(self, is_long: bool, count: int, sig: PaBarSnapshot, quality_ok: bool,
                         risk: RiskResult) -> None:
        # port of EntryCount.DrawEntryLabel, lines 1534-1548
        cfg = self.cfg
        if (not cfg.ShowLabels or count > cfg.MaxLabeledCount or (count == 1 and not cfg.ShowFirstEntries)
                or not (cfg.ShowLongCount if is_long else cfg.ShowShortCount)):
            return
        text = f"{count}{'EL' if is_long else 'ES'}"
        if cfg.ShowRiskTicks:
            text += f" {risk.RiskTicks}T{StopRiskCalculator.class_suffix(risk.RiskClass)}"
        color = cfg.QualityFailColor if cfg.ShowQualityColoring and not quality_ok else (
            cfg.LongColor if is_long else cfg.ShortColor)
        bars_ago = max(0, self.CurrentBar - sig.Index)
        self.draw.put(DrawText(("EC_L_" if is_long else "EC_S_") + str(self.CurrentBar), text,
                               self.CurrentBar - bars_ago, sig.Low if is_long else sig.High,
                               self.label_offset(is_long, 0), color, self.labelFontSize, True))

    def log_entry(self, is_long: bool, count: int, sig: PaBarSnapshot, q: SignalBarResult, risk: RiskResult) -> None:
        # port of EntryCount.LogEntry, lines 1550-1557
        dist = (sig.Low - sig.Ema if is_long else sig.Ema - sig.High) / self.TickSize
        line = self.diag.format("Entry", "Long" if is_long else "Short", count, self.barNumbering.get(sig.Index),
                                sig.High, sig.Low, sig.Open, sig.Close, sig.Ema, dist, q.BodyPct, q.ClosePct,
                                q.WickPct, 1 if q.Ok else 0, risk.Trigger, risk.Stop, risk.RiskTicks, risk.RiskClass,
                                "quality " + q.Reason)
        self.emit(line)

    def log_event(self, evt: str, is_long: bool, count: int, bar: PaBarSnapshot, with_signal: bool,
                  reason: str) -> None:
        # port of EntryCount.LogEvent, lines 1559-1578
        if self.diag is None:
            return
        direction = "Long" if is_long else "Short"
        if with_signal:
            dist = (bar.Low - bar.Ema if is_long else bar.Ema - bar.High) / self.TickSize
            line = self.diag.format(evt, direction, count, self.barNumbering.get(bar.Index),
                                    bar.High, bar.Low, bar.Open, bar.Close, bar.Ema, dist, NAN, NAN, NAN, -1,
                                    NAN, NAN, 0, RISK_NONE, reason)
        else:
            line = self.diag.format(evt, direction, count, 0,
                                    NAN, NAN, NAN, NAN, bar.Ema, NAN, NAN, NAN, NAN, -1,
                                    NAN, NAN, 0, RISK_NONE, reason)
        self.emit(line)

    def emit(self, line: str) -> None:
        # port of EntryCount.Emit, lines 1580-1590
        if self.cfg.DebugLog:
            self.printer(line)
        if self.cfg.ExportCsv:
            self.diag.write_csv(self.tradingDay, line)
            if self.diag.LastError is not None and self.cfg.DebugLog:
                self.printer("EntryCount CSV: " + self.diag.LastError)

