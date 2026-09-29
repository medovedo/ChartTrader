"""Port of the NinjaTrader strategy LotsenhofSniper to the replay engine.

Structure as in the original (LotsenhofSniper.cs):
- Pure calculation logic without broker dependency: signal bar filter, inside bar,
  Smart/Deep/Momentum setup, swing trail. All testable against bar objects with
  open/high/low/close.
- `Sniper` is the state machine (Trap, tracked ATM trades). The original's daily loss lockout
  is deliberately not ported: in the replay trading continues after a losing trade.
  It runs per tick in `ManualStrategy.on_trade_tick`, i.e. inside the Nautilus stream,
  and drives the orders through a few broker methods of the strategy.

Bar convention as in NinjaScript: Bar[0] = forming bar, Bar[1] = last
closed bar (signal bar), Bar[2] = the one before. `cur` = index of the forming bar.

An NT ATM with two brackets is mapped to two Nautilus bracket order lists
(entry limit + stop + target per bracket, same entry price).
Bracket 0 = Target1/Stop1, Bracket 1 = Runner (Target2/Stop2).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from atm_templates import AtmTemplate, load_atm_template, max_risk_from_name

CHICAGO = ZoneInfo("America/Chicago")


# --------------------------------------------------------------------------- Configuration
@dataclass
class SniperConfig:
    """Parameters with the defaults from LotsenhofSniper.OnStateChange (SetDefaults)."""
    atm_templates: tuple[str, ...] = ("WADES12", "WADES10", "WADES14", "WADES16", "WADES8", "WADES6")
    no_runner_suffix: str = "NR"
    atm_dir: Path | None = None            # None = NinjaTrader default folder
    max_pullback_percent: float = 49.0
    block_inside_bars: bool = True
    max_slippage_ticks: int = 2            # Momentum: limit offset from the stop price
    default_max_risk_ticks: int = 9        # if the ATM name contains no number
    risk_tolerance_ticks: int = 2          # "rubber band" above Max Risk before the limit is moved deeper
    max_runway_ticks: int = 10             # cancel unfilled entry when price runs this far away
    bars_to_wait: int = 3                  # cancel unfilled entry after this many bars
    partial_fill_cancel_ticks: int = 10    # cancel remainder of a partially filled entry after this many ticks of profit
    enable_non_perfect_bars: bool = False  # True = signal bar filter completely off
    require_close_position: bool = True
    close_position_percent: float = 70.0
    require_min_body: bool = True
    min_body_percent: float = 5.0
    require_no_breakout_wick: bool = True
    max_breakout_wick_percent: float = 25.0
    enable_auto_breakeven: bool = True     # after Target1 fill move runner stop to breakeven
    auto_breakeven_offset_ticks: int = 0
    enable_runner_trail: bool = True
    swing_strength: int = 2
    min_swing_size_ticks: int = 8
    trail_buffer_ticks: int = 1
    scratch_offset_ticks: int = 2
    show_only_smart_buttons: bool = True


class SetupRejected(Exception):
    """Entry rejected; the message matches the NT text on the chart."""


@dataclass(frozen=True)
class Setup:
    is_long: bool
    trigger: float          # Smart/Deep: price at which the Trap fires. Momentum: stop price of the entry
    limit: float            # limit price of the entry order
    stop: float             # intended stop after the fill (structural stop, capped at Max Risk if needed)
    note: str = ""


# --------------------------------------------------------------------------- pure calculation logic
def signal_bar_check(is_long: bool, o: float, h: float, l: float, c: float, tick: float,
                     cfg: SniperConfig) -> tuple[bool, str]:
    """SignalBarCheck.Evaluate: close position, minimum body, breakout wick. A shaved bar skips body/wick."""
    if cfg.enable_non_perfect_bars:
        return True, ""
    rng = h - l
    if rng <= 0:
        return False, "Signal bar rejected: zero range"
    close_pos = (c - l if is_long else h - c) / rng * 100.0
    body = abs(c - o) / rng * 100.0
    wick = (h - c if is_long else c - l) / rng * 100.0
    shaved = c >= h - tick / 2.0 if is_long else c <= l + tick / 2.0
    failed = []
    if cfg.require_close_position and close_pos < cfg.close_position_percent:
        failed.append(f"close {close_pos:.1f}% < {cfg.close_position_percent:g}%")
    if cfg.require_min_body and not shaved and body < cfg.min_body_percent:
        failed.append(f"body {body:.1f}% < {cfg.min_body_percent:g}%")
    if cfg.require_no_breakout_wick and not shaved and wick > cfg.max_breakout_wick_percent:
        failed.append(f"breakout wick {wick:.1f}% > {cfg.max_breakout_wick_percent:g}%")
    if failed:
        return False, "Signal bar rejected: " + " | ".join(failed)
    return True, ""


def is_inside_bar(b1: Any, b2: Any) -> bool:
    return b1.high <= b2.high and b1.low >= b2.low


def _common_checks(is_long: bool, b1: Any, b2: Any, tick: float, cfg: SniperConfig) -> None:
    if cfg.block_inside_bars and is_inside_bar(b1, b2):
        raise SetupRejected("ERROR: Inside Bar blocked!")
    ok, reason = signal_bar_check(is_long, b1.open, b1.high, b1.low, b1.close, tick, cfg)
    if not ok:
        raise SetupRejected(reason)


def smart_setup(is_long: bool, b1: Any, b2: Any, tick: float, max_risk: int, cfg: SniperConfig) -> Setup:
    """ExecuteSmartLong/Short: stop beyond the two-bar extreme, trigger 1 tick above/below the signal bar.
    If the risk fits, buy directly at the trigger; otherwise place the limit deeper, provided the required
    pullback does not exceed Max Pullback % of the structure."""
    _common_checks(is_long, b1, b2, tick, cfg)
    if is_long:
        stop = min(b1.low, b2.low) - tick
        trigger = b1.high + tick
        risk = round((trigger - stop) / tick)
        structure = round((b1.high - min(b1.low, b2.low)) / tick)
    else:
        stop = max(b1.high, b2.high) + tick
        trigger = b1.low - tick
        risk = round((stop - trigger) / tick)
        structure = round((max(b1.high, b2.high) - b1.low) / tick)
    if risk > max_risk + cfg.risk_tolerance_ticks:
        required = risk - max_risk
        allowed = math.floor(structure * cfg.max_pullback_percent / 100.0)
        if required > allowed:
            raise SetupRejected("ERROR: Double bar structure too large!\n"
                                f"Required risk pullback ({required:g} ticks) exceeds max allowed limit ({allowed:g} ticks).")
        limit = stop + max_risk * tick if is_long else stop - max_risk * tick
        note = f"pullback entry, risk {risk:g} > {max_risk} ticks"
    else:
        limit = trigger
        note = "Risk OK. Direct trigger entry."
    return Setup(is_long, trigger, limit, stop, note)


def deep_setup(is_long: bool, b1: Any, b2: Any, tick: float, max_risk: int, cfg: SniperConfig) -> Setup:
    """ExecuteDeepSmartLong/Short: limit always Max Pullback % into the signal bar,
    if the risk is too large even deeper at stop + Max Risk."""
    _common_checks(is_long, b1, b2, tick, cfg)
    candle = round((b1.high - b1.low) / tick)
    pullback = math.floor(candle * cfg.max_pullback_percent / 100.0)
    if is_long:
        stop = min(b1.low, b2.low) - tick
        trigger = b1.high + tick
        limit = b1.high - pullback * tick
        deep_risk = round((limit - stop) / tick)
        if deep_risk > max_risk:
            limit = stop + max_risk * tick
            note = "Deep Long pulled deeper to meet strict Max Risk requirements."
        else:
            note = f"deep entry {pullback:g} ticks into signal bar"
    else:
        stop = max(b1.high, b2.high) + tick
        trigger = b1.low - tick
        limit = b1.low + pullback * tick
        deep_risk = round((stop - limit) / tick)
        if deep_risk > max_risk:
            limit = stop - max_risk * tick
            note = "Deep Short pulled deeper to meet strict Max Risk requirements."
        else:
            note = f"deep entry {pullback:g} ticks into signal bar"
    return Setup(is_long, trigger, limit, stop, note)


def momentum_setup(is_long: bool, b1: Any, b2: Any, tick: float, max_risk: int, cfg: SniperConfig) -> Setup:
    """ExecuteMomentumLong/Short: entry 1 tick above/below the signal bar as stop-limit with
    Max Slippage; stop beyond the signal bar, capped at Max Risk (no tolerance, no pullback)."""
    _common_checks(is_long, b1, b2, tick, cfg)
    if is_long:
        entry = b1.high + tick
        limit = entry + cfg.max_slippage_ticks * tick
        stop = b1.low - tick
        if (entry - stop) / tick > max_risk:
            stop = entry - max_risk * tick
    else:
        entry = b1.low - tick
        limit = entry - cfg.max_slippage_ticks * tick
        stop = b1.high + tick
        if (stop - entry) / tick > max_risk:
            stop = entry + max_risk * tick
    return Setup(is_long, entry, limit, stop, "momentum")


def swing_trail_candidate(is_long: bool, series: list[Any], setup_bar: int, current_runner_stop: float,
                          tick: float, cfg: SniperConfig) -> float | None:
    """ManageRunnerTrail: most recent confirmed swing after the setup (Strength bars per side,
    newer side strict, older side may be equal), minimum prominence, buffer; only tighter."""
    n = len(series)                       # series[-1] = Bar[0]
    cur = n - 1
    if cur < 2 * cfg.swing_strength + 2:
        return None
    s = cfg.swing_strength
    lookback = min(cur - s - 1, 100)
    close0 = series[-1].close
    for p in range(s + 1, lookback + 1):
        if cur - p <= setup_bar:
            break
        bar_p = series[-1 - p]
        if is_long:
            piv = bar_p.low
            ok = all(series[-1 - (p - k)].low > piv and series[-1 - (p + k)].low >= piv for k in range(1, s + 1))
            if not ok:
                continue
            max_since = max(series[-1 - j].high for j in range(p))
            if (max_since - piv) / tick < cfg.min_swing_size_ticks:
                continue
            cand = piv - cfg.trail_buffer_ticks * tick
            if cand <= current_runner_stop or cand >= close0:
                return None
            return cand
        else:
            piv = bar_p.high
            ok = all(series[-1 - (p - k)].high < piv and series[-1 - (p + k)].high <= piv for k in range(1, s + 1))
            if not ok:
                continue
            min_since = min(series[-1 - j].low for j in range(p))
            if (piv - min_since) / tick < cfg.min_swing_size_ticks:
                continue
            cand = piv + cfg.trail_buffer_ticks * tick
            if (current_runner_stop != 0 and cand >= current_runner_stop) or cand <= close0:
                return None
            return cand
    return None


def trading_day(ts_ns: int):
    """CME trading day: the session from 17:00 Chicago counts toward the next calendar day."""
    return (datetime.fromtimestamp(ts_ns / 1e9, tz=CHICAGO) + timedelta(hours=7)).date()


# --------------------------------------------------------------------------- broker interface
@dataclass(frozen=True)
class OrderView:
    status: str          # Nautilus OrderStatus name, e.g. ACCEPTED, PARTIALLY_FILLED, FILLED, CANCELED
    filled_qty: float
    leaves_qty: float
    avg_px: float
    price: float         # limit price (0 if none)
    trigger_price: float # stop trigger (0 if none)
    is_open: bool
    is_closed: bool
    quantity: float = 0.0  # order quantity (leaves_qty can be stale after the exchange changes the quantity)


class Broker(Protocol):
    """Implemented by ManualStrategy; every order is addressed by its ID."""
    def place_bracket(self, is_long: bool, qty: int, limit_price: float, stop_price: float,
                      target_price: float, trigger_price: float | None = None) -> tuple[Any, Any, Any]: ...
    def place_exits(self, is_long: bool, qty: int, stop_price: float, target_price: float) -> tuple[Any, Any]: ...
    def order_view(self, oid: Any) -> OrderView: ...
    def modify(self, oid: Any, price: float | None = None, trigger_price: float | None = None,
               quantity: int | None = None) -> None: ...
    def cancel(self, oid: Any) -> None: ...
    def close_all(self) -> None: ...
    def log(self, msg: str) -> None: ...


# --------------------------------------------------------------------------- state
@dataclass
class TrackedBracket:
    entry: Any
    sl: Any
    tp: Any
    quantity: int
    be_trigger_ticks: int
    be_plus_ticks: int
    is_runner: bool
    atm_be_done: bool = False
    targets_aligned: bool = False


@dataclass
class AtmTrade:
    """Corresponds to AtmState in the original."""
    is_long: bool
    template: AtmTemplate
    brackets: list[TrackedBracket]
    intended_stop: float
    trigger: float
    setup_bar: int
    label: str
    entry_price: float = 0.0
    entry_complete: bool = False
    entry_cancel_requested: bool = False
    post_fill_extreme: float = 0.0     # best last price since the fill (partial fill cancel, as in the original)
    unprotected_warned: bool = False
    position_seen: bool = False
    stop1_adjusted: bool = False
    runner_stop_adjusted: bool = False
    stop_notified: bool = False
    be_triggered: bool = False
    t1_filled: bool = False            # Target1 filled -> runner trail may run (independent of auto-BE)
    current_runner_stop: float = 0.0
    scratch_target: float = 0.0

    @property
    def has_runner(self) -> bool:
        return len(self.brackets) > 1

    @property
    def sign(self) -> int:
        return 1 if self.is_long else -1


@dataclass
class Trap:
    """Armed entry: fires as soon as Bar[0] reaches the trigger; expires with the next bar."""
    is_long: bool
    trigger: float
    limit: float
    stop: float
    bar: int
    template: AtmTemplate
    label: str


class Sniper:
    def __init__(self, broker: Broker, tick_size: float, cfg: SniperConfig | None = None):
        self.b = broker
        self.tick = tick_size
        self.cfg = cfg or SniperConfig()
        self.atm_index = 0
        self.max_risk = max_risk_from_name(self.atm_name, self.cfg.default_max_risk_ticks)
        self.trap: Trap | None = None
        self.trades: list[AtmTrade] = []
        self.notice = ""            # last rejection/error message for display
        self.bar0: Any = None
        self.bars: list[Any] = []
        self.cur = 0
        self._last_cur = -1

    # --- ATM selection --------------------------------------------------------
    @property
    def atm_name(self) -> str:
        return self.cfg.atm_templates[self.atm_index]

    def next_atm(self) -> str:
        self.atm_index = (self.atm_index + 1) % len(self.cfg.atm_templates)
        self.max_risk = max_risk_from_name(self.atm_name, self.cfg.default_max_risk_ticks)
        self._log(f"ATM switched to {self.atm_name}, max risk {self.max_risk} ticks")
        return self.atm_name

    def _template(self, no_runner: bool) -> AtmTemplate | None:
        name = self.atm_name + self.cfg.no_runner_suffix if no_runner else self.atm_name
        try:
            return load_atm_template(name, self.cfg.atm_dir)
        except (FileNotFoundError, ValueError) as e:
            self._fail(f"ERROR: ATM template '{name}' not found!\n{e}")
            return None

    # --- messages -------------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.b.log("SNIPER " + msg)

    def _fail(self, msg: str) -> None:
        self.notice = msg
        self._log(msg.replace("\n", " "))

    # --- preconditions --------------------------------------------------------
    def _signal_bars(self):
        if len(self.bars) < 2:
            self._fail("ERROR: not enough bars yet")
            return None
        return self.bars[-1], self.bars[-2]

    # --- actions from GUI/hotkeys ------------------------------------------------
    def arm_smart(self, is_long: bool, no_runner: bool = False) -> bool:
        if no_runner and not self.cfg.no_runner_suffix:
            self._fail("ERROR: No-Runner Template Suffix is empty!")
            return False
        sb = self._signal_bars()
        if sb is None:
            return False
        try:
            setup = smart_setup(is_long, sb[0], sb[1], self.tick, self.max_risk, self.cfg)
        except SetupRejected as e:
            self._fail(str(e))
            return False
        tpl = self._template(no_runner)
        if tpl is None:
            return False
        label = ("Smart Long" if is_long else "Smart Short") + (" (No Runner)" if no_runner else "")
        return self._arm(setup, tpl, label)

    def arm_deep(self, is_long: bool) -> bool:
        sb = self._signal_bars()
        if sb is None:
            return False
        try:
            setup = deep_setup(is_long, sb[0], sb[1], self.tick, self.max_risk, self.cfg)
        except SetupRejected as e:
            self._fail(str(e))
            return False
        tpl = self._template(False)
        if tpl is None:
            return False
        return self._arm(setup, tpl, "Deep Long" if is_long else "Deep Short")

    def _arm(self, setup: Setup, tpl: AtmTemplate, label: str) -> bool:
        self.trap = Trap(setup.is_long, setup.trigger, setup.limit, setup.stop, self.cur, tpl, label)
        self.notice = ""
        self._log(f"{label} Trap Armed. Trigger: {setup.trigger}, Limit: {setup.limit}, SL: {setup.stop}, "
                  f"ATM: {tpl.name} ({setup.note})")
        return True

    def momentum(self, is_long: bool) -> bool:
        """Immediate entry: limit if the market is already beyond the entry, otherwise stop-limit."""
        sb = self._signal_bars()
        if sb is None or self.bar0 is None:
            return False
        try:
            setup = momentum_setup(is_long, sb[0], sb[1], self.tick, self.max_risk, self.cfg)
        except SetupRejected as e:
            self._fail(str(e))
            return False
        tpl = self._template(False)
        if tpl is None:
            return False
        c0 = self.bar0.close
        crossed = c0 >= setup.trigger if is_long else c0 <= setup.trigger
        self.notice = ""
        self._place(setup.is_long, tpl, setup.limit, setup.stop, setup.trigger,
                    None if crossed else setup.trigger, "Momentum Long" if is_long else "Momentum Short")
        return True

    def cancel_all(self) -> None:
        """Killswitch: Trap removed, entry orders cancelled, positions closed."""
        self.trap = None
        self.notice = ""
        for t in self.trades:
            for br in t.brackets:
                self.b.cancel(br.entry)
        self.b.close_all()
        self.trades.clear()
        self._log("Killswitch activated. All traps and active orders destroyed.")

    def scratch(self) -> int:
        """Pull targets of all filled trades to entry +/- Scratch Offset (only tighter)."""
        self.notice = ""
        live = adjusted = 0
        for t in self.trades:
            if t.entry_price == 0 or self._open_qty(t) <= 0:
                continue
            live += 1
            price = t.entry_price + t.sign * self.cfg.scratch_offset_ticks * self.tick
            if t.scratch_target != 0:
                closer = price < t.scratch_target if t.is_long else price > t.scratch_target
                if not closer:
                    continue
            for br in t.brackets:
                if br.is_runner and not t.entry_complete:
                    continue
                if self.b.order_view(br.tp).is_open:
                    self.b.modify(br.tp, price=price)
                    adjusted += 1
            t.scratch_target = price
        if live == 0:
            self._fail("ERROR: No active position to scratch!")
        elif adjusted == 0:
            self._log(f"Scratch skipped: targets already at or tighter than {self.cfg.scratch_offset_ticks} ticks.")
        else:
            self._log(f"SCRATCH: {adjusted} target(s) pulled to {self.cfg.scratch_offset_ticks} ticks beyond entry")
        return adjusted

    # --- order placement ----------------------------------------------------------
    def _place(self, is_long: bool, tpl: AtmTemplate, limit: float, stop: float, trigger: float,
               entry_trigger: float | None, label: str) -> AtmTrade:
        sign = 1 if is_long else -1
        brackets = []
        for i, br in enumerate(tpl.brackets):
            # Stop/target initially relative to the limit; after the fill the stop is moved to the
            # structural stop and the target is aligned to the actual entry price.
            sl = limit - sign * br.stop_ticks * self.tick
            tp = limit + sign * br.target_ticks * self.tick
            e, s, t = self.b.place_bracket(is_long, br.quantity, limit, sl, tp, entry_trigger)
            brackets.append(TrackedBracket(e, s, t, br.quantity, br.be_trigger_ticks, br.be_plus_ticks, i > 0))
        trade = AtmTrade(is_long, tpl, brackets, stop, trigger, self.cur, label)
        self.trades.append(trade)
        kind = "StopLimit" if entry_trigger else "Limit"
        self._log(f"{label}: {kind} {tpl.entry_quantity} @ {limit} ({tpl.name}), intended SL {stop}")
        return trade

    # --- tick processing -----------------------------------------------------------
    def on_tick(self, bar0: Any, bars: list[Any], ts_ns: int) -> None:
        """OnBarUpdate equivalent, call after every tick. bar0 may be None (tick bar just closed)."""
        if bar0 is None:
            if not bars:
                return
            bar0, cur = bars[-1], len(bars) - 1
        else:
            cur = len(bars)
        self.bar0, self.bars, self.cur = bar0, bars, cur
        new_bar = cur != self._last_cur
        self._last_cur = cur

        # Trap: expires with the next bar, fires when the trigger is touched
        tr = self.trap
        if tr and cur > tr.bar:
            self.trap = None
            self._log(f"{tr.label} trap expired (bar closed without trigger).")
        elif tr:
            hit = bar0.high >= tr.trigger if tr.is_long else bar0.low <= tr.trigger
            if hit:
                self.trap = None
                self._place(tr.is_long, tr.template, tr.limit, tr.stop, tr.trigger, None, tr.label)

        for t in list(self.trades):
            if self._manage(t, new_bar):
                self.trades.remove(t)

    def _open_qty(self, t: AtmTrade) -> float:
        qty = 0.0
        for br in t.brackets:
            e = self.b.order_view(br.entry)
            qty += e.filled_qty - self.b.order_view(br.sl).filled_qty - self.b.order_view(br.tp).filled_qty
        return qty

    def _manage(self, t: AtmTrade, new_bar: bool) -> bool:
        """One tracked trade per tick. Returns True once it is finished."""
        b0, tick = self.bar0, self.tick
        views = [self.b.order_view(br.entry) for br in t.brackets]

        if t.entry_price == 0:
            filled = [v for v in views if v.filled_qty > 0]
            if filled:
                t.entry_price = filled[0].avg_px
                self._log(f"{t.label} filled @ {t.entry_price}")
            else:
                run = (b0.high - t.trigger) / tick if t.is_long else (t.trigger - b0.low) / tick
                if run >= self.cfg.max_runway_ticks:
                    self._log(f"{t.label} EXPIRED: price ran {self.cfg.max_runway_ticks} ticks past trigger. Cancelling.")
                    self._cancel_trade(t)
                    return True
                if self.cur >= t.setup_bar + self.cfg.bars_to_wait:
                    self._log(f"{t.label} EXPIRED: allowed bars exceeded (current {self.cur}, setup {t.setup_bar}, "
                              f"allowed {self.cfg.bars_to_wait}). Cancelling.")
                    self._cancel_trade(t)
                    return True
                return False

        open_qty = self._open_qty(t)
        if open_qty > 0:
            t.position_seen = True
        if t.position_seen and open_qty <= 0:
            self._log(f"{t.label} closed.")
            return True
        if not t.entry_complete and all(v.is_closed for v in views):
            t.entry_complete = True

        # Stop/target follow the filled quantity of their entry (like an NT ATM). The exchange creates the
        # OTO children with the full entry quantity, which would overshoot while the entry is partially filled.
        for br, v in zip(t.brackets, views):
            if v.filled_qty > 0:
                for oid in (br.sl, br.tp):
                    ov = self.b.order_view(oid)
                    if ov.is_open and ov.filled_qty == 0 and ov.quantity != v.filled_qty:
                        self.b.modify(oid, quantity=int(v.filled_qty))

        # Partial fill: cancel the remainder when price has run away. Cancelling (or reducing) a partially
        # filled entry makes the exchange cancel its OTO stop/target as well, so the filled contracts get
        # a new OCO pair right away.
        if not t.entry_complete and not t.entry_cancel_requested:
            # Only the way since the fill counts (PostFillExtreme on the last price): the bar's high/low
            # may lie before a pullback fill and would cancel the rest at once
            last = b0.close
            if t.post_fill_extreme == 0:
                t.post_fill_extreme = t.entry_price
            t.post_fill_extreme = max(t.post_fill_extreme, last) if t.is_long else min(t.post_fill_extreme, last)
            run = (t.post_fill_extreme - t.entry_price) / tick * t.sign
            if run >= self.cfg.partial_fill_cancel_ticks:
                t.entry_cancel_requested = True
                for br, v in zip(t.brackets, views):
                    if not v.is_open:
                        continue
                    self.b.cancel(br.entry)
                    if v.filled_qty > 0:
                        self._replace_exits(t, br, v)
                self._log(f"PARTIAL FILL: rest of entry cancelled ({self.cfg.partial_fill_cancel_ticks} ticks run)")

        # Align targets to the actual entry price (fill may differ from the limit, e.g. Momentum)
        for br, v in zip(t.brackets, views):
            if not br.targets_aligned and v.filled_qty > 0:
                tpv = self.b.order_view(br.tp)
                want = self._r(v.avg_px + t.sign * self._target_ticks(t, br) * tick)
                if tpv.is_open and abs(tpv.price - want) > tick / 2:
                    self.b.modify(br.tp, price=want)
                br.targets_aligned = True

        # Structural stop: Stop1 immediately, runner stop only once the entry is complete
        if t.intended_stop > 0:
            b1 = t.brackets[0]
            if not t.stop1_adjusted and self.b.order_view(b1.sl).is_open:
                self.b.modify(b1.sl, trigger_price=t.intended_stop)
                t.stop1_adjusted = True
            if t.has_runner and t.entry_complete and not t.runner_stop_adjusted:
                r = t.brackets[1]
                if self.b.order_view(r.sl).is_open:
                    self.b.modify(r.sl, trigger_price=t.intended_stop)
                    t.runner_stop_adjusted = True
                    t.current_runner_stop = t.intended_stop
            if t.stop1_adjusted and not t.stop_notified:
                t.stop_notified = True
                self._log(f"STRUCTURAL SL: secured at {t.intended_stop}")

        # NinjaTrader's own ATM breakeven (stop strategy of the template), per bracket
        for br in t.brackets:
            if br.be_trigger_ticks > 0 and not br.atm_be_done:
                profit = (b0.high - t.entry_price) / tick if t.is_long else (t.entry_price - b0.low) / tick
                if profit >= br.be_trigger_ticks:
                    be = self._r(t.entry_price + t.sign * br.be_plus_ticks * tick)
                    self._tighten_stop(t, br, be, "ATM-BE")
                    br.atm_be_done = True

        # Sniper auto breakeven: Target1 filled -> runner stop to breakeven
        if self.cfg.enable_auto_breakeven and t.has_runner and not t.be_triggered:
            if self.b.order_view(t.brackets[0].tp).status == "FILLED":
                be = self._r(t.entry_price + t.sign * self.cfg.auto_breakeven_offset_ticks * tick)
                self._tighten_stop(t, t.brackets[1], be, "AUTO-BE")
                t.be_triggered = True

        # Runner swing trail once Target1 is filled (independent of auto-BE, as in the original), once per new bar
        if self.cfg.enable_runner_trail and t.has_runner and not t.t1_filled:
            t.t1_filled = self.b.order_view(t.brackets[0].tp).status == "FILLED"
        if self.cfg.enable_runner_trail and (t.be_triggered or t.t1_filled) and new_bar and t.has_runner:
            cand = swing_trail_candidate(t.is_long, self.bars + [b0], t.setup_bar, t.current_runner_stop,
                                         tick, self.cfg)
            if cand is not None:
                self._tighten_stop(t, t.brackets[1], cand, "TRAIL")

        if t.entry_complete and open_qty <= 0:
            self._log(f"{t.label} closed.")
            return True
        # Safety net: filled contracts without any working stop must never go unnoticed
        if t.entry_complete and open_qty > 0 and not t.unprotected_warned:
            protected = sum(self.b.order_view(br.sl).quantity for br in t.brackets if self.b.order_view(br.sl).is_open)
            if protected < open_qty:
                t.unprotected_warned = True
                self._log(f"WARNING {t.label}: {open_qty:g} contract(s) open, only {protected:g} covered by a stop!")
        return False

    def _replace_exits(self, t: AtmTrade, br: TrackedBracket, entry: OrderView) -> None:
        """New OCO stop/target for the filled part of a cancelled entry. Keeps prices that were already set
        (structural stop, manual moves, aligned target), otherwise the intended stop / target from the fill."""
        tick = self.tick
        old_sl, old_tp = self.b.order_view(br.sl), self.b.order_view(br.tp)
        adjusted = t.runner_stop_adjusted if br.is_runner else t.stop1_adjusted
        stop = old_sl.trigger_price if adjusted or t.intended_stop <= 0 else t.intended_stop
        target = old_tp.price if br.targets_aligned else \
            self._r(entry.avg_px + t.sign * self._target_ticks(t, br) * tick)
        for oid in (br.sl, br.tp):
            self.b.cancel(oid)                        # the exchange cancels them with the entry anyway
        br.sl, br.tp = self.b.place_exits(t.is_long, int(entry.filled_qty), stop, target)
        br.targets_aligned = True
        if br.is_runner:
            t.runner_stop_adjusted = True
            t.current_runner_stop = stop
        else:
            t.stop1_adjusted = True
        self._log(f"{'Runner' if br.is_runner else 'Bracket'} {int(entry.filled_qty)} contract(s): new stop {stop}, "
                  f"target {target}")

    def _target_ticks(self, t: AtmTrade, br: TrackedBracket) -> int:
        return t.template.brackets[t.brackets.index(br)].target_ticks

    def _tighten_stop(self, t: AtmTrade, br: TrackedBracket, price: float, why: str) -> None:
        v = self.b.order_view(br.sl)
        if not v.is_open:
            return
        better = price > v.trigger_price if t.is_long else price < v.trigger_price
        if not better:
            return
        self.b.modify(br.sl, trigger_price=price)
        if br.is_runner:
            t.current_runner_stop = price
        self._log(f"{why}: {'Stop2' if br.is_runner else 'Stop1'} moved to {price}")

    def on_manual_move(self, oid: Any, price: float) -> None:
        """Stop/target dragged in the chart: keep it, the Sniper only tightens from there
        (no later structural stop or target alignment overwrites it)."""
        for t in self.trades:
            for br in t.brackets:
                if br.sl == oid:
                    if br.is_runner:
                        t.current_runner_stop = price
                        t.runner_stop_adjusted = True
                    else:
                        t.stop1_adjusted = t.stop_notified = True
                elif br.tp == oid:
                    br.targets_aligned = True

    def _cancel_trade(self, t: AtmTrade) -> None:
        for br in t.brackets:
            self.b.cancel(br.entry)

    def _r(self, p: float) -> float:
        return round(round(p / self.tick) * self.tick, 10)
