"""Support code for the ports of the NinjaTrader indicators EntryCount and DoubleTrap (no NT logic here).

- `PaBar`: one closed bar as NinjaScript sees it (Time[0] = bar END time in NT's time zone).
- .NET Framework number formatting (NT 8 runs on .NET Framework 4.8): custom formats like "0.00##"
  first round the double to 15 significant digits, then round half away from zero, and never print "-0".
  `double.ToString()` / "{0}" is the "G" format with 15 significant digits.
- `Drawings`: records Draw.Text / Draw.Dot / Draw.Line / Draw.Diamond calls by tag (a new call with the same tag
  replaces the object, like NinjaTrader), with absolute bar indices instead of barsAgo. The chart overlay reads it.
- `trading_day_of`: CME trading day (session from 17:00 Chicago counts toward the next day), the value of
  SessionIterator.ActualTradingDayExchange that names the CSV files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")


@dataclass
class PaBar:
    open: float
    high: float
    low: float
    close: float
    time: datetime          # bar end (time of the last tick), tz-aware in NT's time zone
    volume: float = 0.0


def trading_day_of(t: datetime) -> date:
    return (t.astimezone(CHICAGO) + timedelta(hours=7)).date()


# --------------------------------------------------------------------------- .NET Framework formatting
def _dec15(x: float) -> Decimal:
    return Decimal(format(x, ".15g"))


def net_custom(x: float, min_dec: int, max_dec: int, decimal: str = ",") -> str:
    """Custom numeric format like "0", "0.#", "0.00##" (no grouping)."""
    q = _dec15(x).quantize(Decimal(1).scaleb(-max_dec), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    s = f"{q:f}"
    if max_dec > min_dec and "." in s:
        head, frac = s.split(".")
        frac = frac.rstrip("0")
        if len(frac) < min_dec:
            frac = frac + "0" * (min_dec - len(frac))
        s = head + ("." + frac if frac else "")
    return s.replace(".", decimal)


def net_g(x: float) -> str:
    """double.ToString() with invariant culture (G, 15 significant digits)."""
    s = format(x, ".15g")
    if s in ("-0", "-0.0"):
        return "0"
    if "e" in s:
        mant, exp = s.split("e")
        sign = exp[0]
        digits = exp[1:].lstrip("0").rjust(2, "0")
        s = f"{mant}E{sign}{digits}"
    return s


def net_value(v) -> str:
    """string.Format("{0}", v) for the types used in the indicator messages."""
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return net_g(v)
    return str(v)


def net_datetime(t: datetime) -> str:
    """ToString("dd.MM.yyyy HH:mm:ss")."""
    return t.strftime("%d.%m.%Y %H:%M:%S")


# --------------------------------------------------------------------------- drawing objects
@dataclass
class DrawText:
    tag: str
    text: str
    bar: int            # absolute bar index (CurrentBar - barsAgo)
    y: float
    offset_px: int      # positive = up (NinjaTrader yPixelOffset; text hangs down from its anchor)
    color: str
    font_size: int
    bold: bool


@dataclass
class DrawDot:
    tag: str
    bar: int
    y: float
    color: str


@dataclass
class DrawDiamond:
    tag: str
    bar: int
    y: float
    color: str


@dataclass
class DrawLine:
    tag: str
    bar1: int
    y1: float
    bar2: int
    y2: float
    color: str
    dashed: bool
    width: int


@dataclass
class Drawings:
    """Draw.* calls of one indicator, keyed by tag like in NinjaTrader."""
    objects: dict = field(default_factory=dict)
    version: int = 0     # increases with every change (the chart redraws only then)

    def put(self, obj) -> None:
        self.objects[obj.tag] = obj
        self.version += 1

    def clear(self) -> None:
        self.objects.clear()
        self.version += 1
