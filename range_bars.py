"""Bar aggregators: NinjaTrader-style range bars and tick bars.

Range bar: spans exactly `range_ticks` ticks (high - low = range_ticks * tick_size).
As soon as a trade leaves the range, the bar is closed at the edge and the
next bar opens at the close of the previous bar (no phantom gap).

Tick bar: closes after `ticks_per_bar` trades; the next bar opens with the
following trade (`current` is None in between).

Trading day: at the CME session start (17:00 Chicago) the running bar is closed,
even if it is not full yet, and bar numbering restarts at 1.
`day_breaks` holds the index of the first bar of each new trading day,
`bar_numbers` the number of every closed bar, `current_no` that of the running one.

All share the same interface: `update()` returns the bars closed in this step,
`bars` are all closed bars, `current` is the running one,
`fresh()` returns an empty aggregator with the same parameters.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
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
    current_no: int = 0
    bar_numbers: list = field(default_factory=list)
    day_breaks: list = field(default_factory=list)

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
                self.bar_numbers.append(self.current_no)
                closed.append(b)
                self.current = None
                self._on_session_close()
            self.day_breaks.append(len(self.bars))
            self.current_no = 0
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
        self.bar_numbers.append(self.current_no)
        self.current = None
        self._on_session_close()
        return [b]

    def number_of(self, i: int) -> int | None:
        """Session number of the bar at index i (closed and running bar)."""
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
            self.current_no += 1
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
            self.bar_numbers.append(self.current_no)
            closed.append(bar)
            # open new bar at the close of the old one
            o = bar.close
            bar = RangeBar(o, o, o, o, 0.0, ts, ts)
            self.current = bar
            self.current_no += 1
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
            self.current_no += 1
        else:
            b = self.current
            b.high, b.low, b.close, b.ts_close = max(b.high, price), min(b.low, price), price, ts
            b.volume += size
            self.count += 1
        if self.count >= self.ticks_per_bar:
            b = self.current
            b.closed = True
            self.bars.append(b)
            self.bar_numbers.append(self.current_no)
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
            self.current_no += 1
        else:
            b = self.current
            b.high, b.low, b.close, b.ts_close = max(b.high, price), min(b.low, price), price, ts
            b.volume += size
            self.count += size
        if self.count >= self.contracts_per_bar:
            b = self.current
            b.closed = True
            self.bars.append(b)
            self.bar_numbers.append(self.current_no)
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
