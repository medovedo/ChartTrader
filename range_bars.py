"""Bar aggregators: NinjaTrader-style range bars and tick bars.

Range bar: spans exactly `range_ticks` ticks (high - low = range_ticks * tick_size).
As soon as a trade leaves the range, the bar is closed at the edge and the
next bar opens at the close of the previous bar (no phantom gap).

Tick bar: closes after `ticks_per_bar` trades; the next bar opens with the
following trade (`current` is None in between).

Trading day: at the CME session start (17:00 Chicago) the running bar is closed,
even if it is not full yet. `day_breaks` holds the index of the first bar of each new trading day.
Bar numbers restart at 1 on every new calendar date in Berlin (bar end time), like the
DayBarNumbering of the NT indicators EntryCount/DoubleTrap, so chart and CSV show the same numbers.
`bar_numbers` holds the number of every closed bar, `current_no` that of the running one.

All share the same interface: `update()` returns the bars closed in this step,
`bars` are all closed bars, `current` is the running one,
`fresh()` returns an empty aggregator with the same parameters.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
NUMBER_TZ = ZoneInfo("Europe/Berlin")   # bar numbers restart at midnight here (NT's time zone, [ChartTrader] TimeZone)
SESSION_START_HOUR = 17     # CME Globex: new trading day from 17:00 Chicago


def next_session_start_ns(ts_ns: int) -> int:
    """Timestamp (ns) of the next session start after ts_ns (DST-safe via calendar date)."""
    dt = datetime.fromtimestamp(ts_ns / 1e9, tz=CHICAGO)
    start = datetime(dt.year, dt.month, dt.day, SESSION_START_HOUR, tzinfo=CHICAGO)
    if dt >= start:
        d = dt.date() + timedelta(days=1)
        start = datetime(d.year, d.month, d.day, SESSION_START_HOUR, tzinfo=CHICAGO)
    return int(start.timestamp() * 1e9)


@dataclass
class RangeBar:
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts_open: int  # ns
    ts_close: int  # ns
    closed: bool = False


@dataclass
class _SessionMixin:
    """Shared session and numbering logic (fields are inherited by the aggregators)."""
    next_session: int = 0
    bar_numbers: list = field(default_factory=list)
    day_breaks: list = field(default_factory=list)

    def _number(self, ts_close: int) -> int:
        """Number of a bar ending at ts_close that follows the last closed bar (DayBarNumbering: 1 on a new
        calendar date, else previous + 1)."""
        if not self.bar_numbers:
            return 1
        day = datetime.fromtimestamp(ts_close / 1e9, tz=NUMBER_TZ).date()
        prev = datetime.fromtimestamp(self.bars[len(self.bar_numbers) - 1].ts_close / 1e9, tz=NUMBER_TZ).date()
        return 1 if day != prev else self.bar_numbers[-1] + 1

    def _close_number(self, b) -> None:
        """Number the bar b that was just appended to bars."""
        self.bar_numbers.append(self._number(b.ts_close))

    @property
    def current_no(self) -> int:
        """Number of the running bar (end = its last trade so far; 0 without a running bar)."""
        return self._number(self.current.ts_close) if self.current is not None else 0

    def _session_check(self, ts: int) -> list:
        """Before each trade: on session change close the running bar and restart numbering."""
        closed = []
        if self.next_session == 0:
            self.next_session = next_session_start_ns(ts)
            return closed
        if ts >= self.next_session:
            if self.current is not None:
                b = self.current
                b.closed = True
                self.bars.append(b)
                self._close_number(b)
                closed.append(b)
                self.current = None
                self._on_session_close()
            self.day_breaks.append(len(self.bars))
            self.next_session = next_session_start_ns(ts)
        return closed

    def _on_session_close(self) -> None:
        pass

    def end_session(self) -> list:
        """Close the running bar because its session is over and no further trade follows (e.g. Friday);
        the next trade then only starts the new session."""
        if self.current is None:
            return []
        b = self.current
        b.closed = True
        self.bars.append(b)
        self._close_number(b)
        self.current = None
        self._on_session_close()
        return [b]

    def number_of(self, i: int) -> int | None:
        """Day number of the bar at index i (closed and running bar)."""
        if 0 <= i < len(self.bar_numbers):
            return self.bar_numbers[i]
        if i == len(self.bars) and self.current is not None:
            return self.current_no
        return None


@dataclass
class RangeBarAggregator(_SessionMixin):
    tick_size: float = 0.25
    range_ticks: int = 4
    current: RangeBar | None = None
    bars: list = field(default_factory=list)

    @property
    def span(self) -> float:
        return self.range_ticks * self.tick_size

    @property
    def label(self) -> str:
        return f"Range {self.range_ticks}"

    def fresh(self) -> "RangeBarAggregator":
        return RangeBarAggregator(tick_size=self.tick_size, range_ticks=self.range_ticks)

    def _r(self, p: float) -> float:
        return round(round(p / self.tick_size) * self.tick_size, 10)

    def update(self, price: float, size: float, ts: int) -> list:
        """Process one trade; returns the bars closed in this step."""
        price = self._r(price)
        closed = self._session_check(ts)
        if self.current is None:
            self.current = RangeBar(price, price, price, price, size, ts, ts)
            return closed
        bar = self.current
        # on a gap the price can run through several bars
        while True:
            hi = max(bar.high, price)
            lo = min(bar.low, price)
            if self._r(hi - lo) <= self.span + 1e-12:
                bar.high, bar.low, bar.close, bar.ts_close = hi, lo, price, ts
                bar.volume += size
                return closed
            # close bar at the edge
            if price > bar.high:
                bar.high = self._r(bar.low + self.span)
                bar.close = bar.high
            else:
                bar.low = self._r(bar.high - self.span)
                bar.close = bar.low
            bar.ts_close = ts
            bar.closed = True
            self.bars.append(bar)
            self._close_number(bar)
            closed.append(bar)
            # open new bar at the close of the old one
            o = bar.close
            bar = RangeBar(o, o, o, o, 0.0, ts, ts)
            self.current = bar
            # loop: check whether the price breaks this bar as well


@dataclass
class TickBarAggregator(_SessionMixin):
    tick_size: float = 0.25
    ticks_per_bar: int = 2000
    current: RangeBar | None = None
    bars: list = field(default_factory=list)
    count: int = 0

    @property
    def label(self) -> str:
        return f"{self.ticks_per_bar} Tick"

    def fresh(self) -> "TickBarAggregator":
        return TickBarAggregator(tick_size=self.tick_size, ticks_per_bar=self.ticks_per_bar)

    def _r(self, p: float) -> float:
        return round(round(p / self.tick_size) * self.tick_size, 10)

    def _on_session_close(self) -> None:
        self.count = 0

    def update(self, price: float, size: float, ts: int) -> list:
        price = self._r(price)
        closed = self._session_check(ts)
        if self.current is None:
            self.current = RangeBar(price, price, price, price, size, ts, ts)
            self.count = 1
        else:
            b = self.current
            b.high, b.low, b.close, b.ts_close = max(b.high, price), min(b.low, price), price, ts
            b.volume += size
            self.count += 1
        if self.count >= self.ticks_per_bar:
            b = self.current
            b.closed = True
            self.bars.append(b)
            self._close_number(b)
            self.current = None
            self.count = 0
            closed.append(b)
        return closed


@dataclass
class VolumeBarAggregator(_SessionMixin):
    """Volume bars: close as soon as `contracts_per_bar` contracts have traded.

    Databento aggregates ~3.5 contracts per trade record on average, whereas NinjaTrader
    counts every single execution as a tick. NT "2000 Tick" therefore roughly equals
    2000 contracts, not 2000 Databento records.
    """
    tick_size: float = 0.25
    contracts_per_bar: int = 2000
    current: RangeBar | None = None
    bars: list = field(default_factory=list)
    count: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.contracts_per_bar} Vol"

    def fresh(self) -> "VolumeBarAggregator":
        return VolumeBarAggregator(tick_size=self.tick_size, contracts_per_bar=self.contracts_per_bar)

    def _r(self, p: float) -> float:
        return round(round(p / self.tick_size) * self.tick_size, 10)

    def _on_session_close(self) -> None:
        self.count = 0.0

    def update(self, price: float, size: float, ts: int) -> list:
        price = self._r(price)
        closed = self._session_check(ts)
        if self.current is None:
            self.current = RangeBar(price, price, price, price, size, ts, ts)
            self.count = size
        else:
            b = self.current
            b.high, b.low, b.close, b.ts_close = max(b.high, price), min(b.low, price), price, ts
            b.volume += size
            self.count += size
        if self.count >= self.contracts_per_bar:
            b = self.current
            b.closed = True
            self.bars.append(b)
            self._close_number(b)
            self.current = None
            self.count = 0.0
            closed.append(b)
        return closed


def make_aggregator(spec: str, tick_size: float):
    """'range:4' -> range bars, 'tick:2000' -> tick bars (records), 'vol:2000' -> volume bars (contracts)."""
    kind, _, n = spec.partition(":")
    n = int(n or 0)
    if kind == "range" and n > 0:
        return RangeBarAggregator(tick_size=tick_size, range_ticks=n)
    if kind == "tick" and n > 0:
        return TickBarAggregator(tick_size=tick_size, ticks_per_bar=n)
    if kind in ("vol", "volume") and n > 0:
        return VolumeBarAggregator(tick_size=tick_size, contracts_per_bar=n)
    raise ValueError(f"Unknown bar spec '{spec}' (expected range:N, tick:N or vol:N)")


class HigherEma:
    """EMA of a higher bar series (e.g. 21 on tick:10000) sampled at the closes of the chart bars.

    A second aggregator runs on the same trades. The value of a chart bar is the EMA as of its last trade,
    the running higher bar counting with its close so far (on a tick:2000 chart every 5th value is the exact
    EMA of a closed 10000-tick bar). `values` is aligned with the chart's closed bars, `current` belongs to
    the running chart bar. `points` = (chart bar index, EMA) at every close of a higher bar: the chart draws
    the line through these exact values only (smooth, no provisional steps) plus a last segment to `current`.
    """

    def __init__(self, spec: str, period: int, tick_size: float):
        self.spec, self.period, self.tick_size = spec, period, tick_size
        self.agg = make_aggregator(spec, tick_size)
        self.k = 2.0 / (period + 1)
        self.ema: float | None = None       # EMA over the closed higher bars
        self.values: list[float] = []
        self.points: list[tuple[int, float]] = []

    def fresh(self) -> "HigherEma":
        return HigherEma(self.spec, self.period, self.tick_size)

    @property
    def label(self) -> str:
        return f"EMA {self.period} ({self.agg.label})"

    @property
    def current(self) -> float | None:
        cur = self.agg.current
        if cur is None:
            return self.ema
        return cur.close if self.ema is None else self.ema + self.k * (cur.close - self.ema)

    def update(self, price: float, size: float, ts: int, chart_closed: list) -> None:
        """One trade; chart_closed = the chart bars this trade closed (from the chart aggregator)."""
        # Chart bars closed by a session change ended with the previous trade: value before this trade
        before = self.current if any(b.ts_close < ts for b in chart_closed) else None
        base = len(self.values)             # chart index of chart_closed[0]; the running bar follows them
        for hb in self.agg.update(price, size, ts):
            self.ema = hb.close if self.ema is None else self.ema + self.k * (hb.close - self.ema)
            # the chart bar that holds the last trade of the higher bar (a session close ended it a trade earlier)
            idx = next((base + j for j, b in enumerate(chart_closed) if b.ts_close == hb.ts_close),
                       base + len(chart_closed))
            self.points.append((idx, self.ema))
        for b in chart_closed:
            v = before if b.ts_close < ts else self.current
            self.values.append(v if v is not None else b.close)
