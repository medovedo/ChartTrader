"""Headless test of the LotsenhofSniper port against the SimulatedExchange (without Qt).

Tick bars with 10 trades per bar and hand-picked price paths, so that signal bar,
trigger, fills, stop adjustment, breakeven, swing trail, partial fill and cancellation
can be checked deterministically. Usage: python test_sniper.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.objects import Price, Quantity

from atm_templates import AtmBracket, AtmTemplate, load_atm_template, max_risk_from_name
from data_loader import es_contract
from range_bars import RangeBar, TickBarAggregator
from replay_engine import ReplayEngine
from sniper import Sniper, SniperConfig, SetupRejected, deep_setup, momentum_setup, signal_bar_check, smart_setup

FAILS: list[str] = []
T0 = 1_765_800_000_000_000_000   # 2025-12-15 ~13:20 UTC, one trading day


def check(cond: bool, msg: str):
    print(f"[{'OK  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def bar(o, h, l, c) -> RangeBar:
    return RangeBar(o, h, l, c, 0, 0, 0, True)


def ticks_from(instrument, prices, t0=T0):
    """One contract per trade; aggressor from price direction (uptick = buyer), otherwise as before.
    Nautilus derives bid/ask from the aggressor sides - wrong sides produce wrong fills."""
    ts = t0
    out = []
    prev = None
    side = AggressorSide.BUYER
    for k, p in enumerate(prices):
        ts += 100_000_000
        if prev is not None and p != prev:
            side = AggressorSide.BUYER if p > prev else AggressorSide.SELLER
        out.append(TradeTick(instrument.id, Price(p, instrument.price_precision), Quantity.from_int(1),
                             side, TradeId(str(k)), ts, ts))
        prev = p
    return out


# ---- Price paths (10 ticks = one bar each; every trade has volume 1) ----------------------------
BAR_A = [100.00, 100.25, 100.50, 100.75, 101.00, 100.75, 100.50, 100.75, 101.00, 100.75]   # O100 H101 L100 C100.75
BAR_B = [100.75, 100.50, 100.75, 101.00, 101.25, 101.50, 101.75, 102.00, 101.75, 101.75]   # signal bar long
BAR_C = [101.75, 101.75, 102.25, 102.25, 102.25, 102.50, 102.75, 103.00, 103.25, 103.50]   # trigger, 3 fills @102.25
BAR_D = [104.00, 104.25, 104.50, 104.75, 105.00, 105.25, 105.50, 105.75, 105.50, 105.25]   # ATM-BE, T1 @105.25
BAR_D2 = [105.25, 105.50, 105.75, 106.00, 105.75, 105.50, 105.25, 105.00, 104.75, 104.50]  # L 104.50
BAR_E = [105.00, 104.75, 104.50, 104.25, 104.00, 104.25, 104.50, 104.75, 105.00, 105.25]   # pivot low 104.00
BAR_F = [105.50, 105.75, 106.00, 106.25, 106.50, 106.25, 106.50, 106.25, 106.50, 106.50]
BAR_G = [106.25, 106.50, 106.75, 106.50, 106.75, 106.50, 106.75, 106.75, 106.50, 106.75]
BAR_H = [106.50, 106.50, 106.00, 105.00, 104.00, 103.75, 103.50, 103.25, 103.00, 103.00]   # trail stop 103.75
BAR_I0 = [103.00, 103.25, 103.00, 102.75, 103.00, 103.25, 103.00, 102.75, 103.00, 103.00]
BAR_I = [103.00, 102.75, 102.50, 102.25, 102.00, 101.75, 101.50, 101.25, 101.00, 101.25]   # signal bar short
BAR_J = [101.25, 101.00, 100.75, 100.75, 100.75, 100.75, 100.50, 100.25, 100.50, 100.75]   # trigger, 3 fills @100.75
BAR_K = [101.50, 101.75, 102.00, 102.25, 102.50, 102.75, 103.00, 103.25, 103.50, 103.50]   # stop 103.50 -> loss
FLAT = [103.50] * 10


class FakeBroker:
    """Broker for exact partial fills (the SimulatedExchange re-matches the rest of an entry against the
    trade-derived book as soon as an order is changed in the fill tick, so partial fills do not persist).
    Like the exchange, cancelling an entry also cancels its stop/target."""
    def __init__(self):
        self.orders, self.children, self.n, self.logs = {}, {}, 0, []

    def _new(self, **kw):
        self.n += 1
        oid = f"O{self.n}"
        self.orders[oid] = {"status": "ACCEPTED", "filled": 0.0, "avg": 0.0, "price": 0.0, "trigger": 0.0, **kw}
        return oid

    def place_bracket(self, is_long, qty, limit_price, stop_price, target_price, trigger_price=None):
        e = self._new(qty=qty, price=limit_price)
        s = self._new(qty=qty, trigger=stop_price)
        t = self._new(qty=qty, price=target_price)
        self.children[e] = (s, t)
        return e, s, t

    def place_exits(self, is_long, qty, stop_price, target_price):
        return self._new(qty=qty, trigger=stop_price), self._new(qty=qty, price=target_price)

    def order_view(self, oid):
        from sniper import OrderView
        o = self.orders[oid]
        is_open = o["status"] in ("ACCEPTED", "PARTIALLY_FILLED")
        return OrderView(o["status"], o["filled"], o["qty"] - o["filled"], o["avg"], o["price"], o["trigger"],
                         is_open, not is_open, float(o["qty"]))

    def modify(self, oid, price=None, trigger_price=None, quantity=None):
        o = self.orders[oid]
        o.update({k: v for k, v in (("price", price), ("trigger", trigger_price), ("qty", quantity)) if v is not None})

    def cancel(self, oid):
        if self.orders[oid]["status"] in ("ACCEPTED", "PARTIALLY_FILLED"):
            self.orders[oid]["status"] = "CANCELED"
            for child in self.children.get(oid, ()):
                self.cancel(child)

    def fill(self, oid, qty, px):
        o = self.orders[oid]
        o["avg"] = (o["avg"] * o["filled"] + px * qty) / (o["filled"] + qty)
        o["filled"] += qty
        o["status"] = "FILLED" if o["filled"] >= o["qty"] else "PARTIALLY_FILLED"

    def close_all(self):
        pass

    def log(self, msg):
        self.logs.append(msg)


class Harness:
    def __init__(self, prices, cfg=None, journal=None):
        self.inst = es_contract()
        self.ticks = ticks_from(self.inst, prices)
        self.eng = ReplayEngine(self.inst, self.ticks, agg=TickBarAggregator(tick_size=0.25, ticks_per_bar=10),
                                sniper_config=cfg, journal_path=journal)
        self.sn = self.eng.sniper
        self.strat = self.eng.strategy

    def run_to(self, idx: int):
        """Plays ticks one by one until idx ticks are processed (tick idx-1 is the last one)."""
        while self.eng.i < idx and not self.eng.finished:
            self.eng.step(1)

    def open_orders(self):
        return self.eng.state().open_orders

    def net(self):
        return self.eng.state().net_qty

    def events(self):
        return list(self.strat.events)

    def views(self, trade):
        return [(self.strat.order_view(b.entry), self.strat.order_view(b.sl), self.strat.order_view(b.tp))
                for b in trade.brackets]


def main() -> int:
    tick, cfg = 0.25, SniperConfig()

    # ---------- 1. pure calculation logic ----------
    ok, why = signal_bar_check(True, 100.75, 102.0, 100.5, 101.75, tick, cfg)
    check(ok, "Signal bar long accepted (close 83%, body 67%, wick 17%)")
    ok, why = signal_bar_check(True, 100.75, 102.0, 100.5, 101.25, tick, cfg)
    check(not ok and "close 50.0% < 70%" in why, f"Signal bar rejected: {why}")
    ok, why = signal_bar_check(True, 101.0, 102.0, 100.0, 102.0, tick, cfg)
    check(ok, "Shaved bar: body/wick check skipped")
    tpl = load_atm_template("WADES12")
    check(tpl.entry_quantity == 3 and tpl.has_runner and tpl.brackets[1].target_ticks == 24
          and tpl.brackets[0].be_trigger_ticks == 0, f"WADES12 from NT XML (no ATM breakeven): {tpl.brackets}")
    tpl_nr = load_atm_template("WADES12NR")
    check(tpl_nr.entry_quantity == 3 and not tpl_nr.has_runner, "WADES12NR: one bracket, no runner")
    check(max_risk_from_name("WADES12", 9) == 11 and max_risk_from_name("Manual", 9) == 9, "Max Risk from template name")

    b2, b1 = bar(100, 101, 100, 100.75), bar(100.75, 102, 100.5, 101.75)
    s = smart_setup(True, b1, b2, tick, 11, cfg)
    check((s.trigger, s.limit, s.stop) == (102.25, 102.25, 99.75), f"Smart Long direct: {s}")
    b2big = bar(98, 101, 98, 100.75)
    s = smart_setup(True, b1, b2big, tick, 11, cfg)
    check((s.trigger, s.limit, s.stop) == (102.25, 100.50, 97.75), f"Smart Long pullback limit: {s}")
    try:
        smart_setup(True, b1, bar(96, 101, 96, 100.75), tick, 11, cfg); check(False, "Structure too large not detected")
    except SetupRejected as e:
        check("too large" in str(e), f"Smart Long rejected: {str(e).splitlines()[0]}")
    try:
        smart_setup(True, bar(100.25, 100.75, 100.25, 100.75), b2, tick, 11, cfg); check(False, "Inside Bar not detected")
    except SetupRejected as e:
        check("Inside Bar" in str(e), "Inside Bar blocked")
    d = deep_setup(True, b1, b2, tick, 11, cfg)   # candle 6 ticks, floor(6*0.49)=2 -> limit 101.50, risk 7
    check((d.trigger, d.limit, d.stop) == (102.25, 101.50, 99.75), f"Deep Long: {d}")
    m = momentum_setup(True, b1, b2, tick, 11, cfg)  # risk (102.25-100.25)/0.25 = 8 <= 11
    check((m.trigger, m.limit, m.stop) == (102.25, 102.75, 100.25), f"Momentum Long: {m}")
    m = momentum_setup(False, bar(103, 103, 101, 101.25), bar(103, 103.25, 102.75, 103), tick, 11, cfg)
    check((m.trigger, m.limit, m.stop) == (100.75, 100.25, 103.25), f"Momentum Short: {m}")

    # ---------- 2. Smart Long: fill, structural stop, T1, Auto-BE, trail, runner exit ----------
    tmp = tempfile.TemporaryDirectory()
    h = Harness(BAR_A + BAR_B + BAR_C + BAR_D + BAR_D2 + BAR_E + BAR_F + BAR_G + BAR_H + FLAT,
                journal=str(Path(tmp.name) / "trade_log.csv"))
    h.run_to(22)                                    # bar C forming, 2 ticks in
    check(h.sn.arm_smart(True), "Smart Long armed")
    tr = h.sn.trap
    check(tr and (tr.trigger, tr.limit, tr.stop, tr.bar) == (102.25, 102.25, 99.75, 2), f"Trap: {tr}")
    h.run_to(23)                                    # tick 102.25 -> trigger
    check(h.sn.trap is None and len(h.sn.trades) == 1, "Trap fired, ATM trade tracked")
    t = h.sn.trades[0]
    check(len(h.open_orders()) >= 2, f"Entry limits in the book: {h.open_orders()}")
    h.run_to(26)                                    # three trades @102.25 -> 3 fills
    check(t.entry_price == 102.25 and h.net() == 3, f"Entry filled @ {t.entry_price}, net={h.net()}")
    h.run_to(30)                                    # bar C finished
    v = h.views(t)
    check(t.stop1_adjusted and v[0][1].trigger_price == 99.75, f"Stop1 at structural stop: {v[0][1].trigger_price}")
    check(t.runner_stop_adjusted and v[1][1].trigger_price == 99.75, f"Stop2 at structural stop: {v[1][1].trigger_price}")
    check(v[0][2].price == 105.25 and v[1][2].price == 108.25, f"Targets +12/+24: {v[0][2].price}, {v[1][2].price}")
    h.strat.move_order(t.brackets[0].sl, 99.50)     # dragged in the chart: Stop1 looser, Stop2 tighter
    h.strat.move_order(t.brackets[1].sl, 101.00)
    h.run_to(31)
    v = h.views(t)
    check(v[0][1].trigger_price == 99.50 and v[1][1].trigger_price == 101.00 and t.current_runner_stop == 101.00,
          f"Manual stop moves kept: Stop1 {v[0][1].trigger_price}, Stop2 {v[1][1].trigger_price}")
    h.run_to(34)                                    # 104.75 = +10 ticks, template has no ATM-BE
    v = h.views(t)
    check(v[0][1].trigger_price == 99.50 and v[1][1].trigger_price == 101.00, f"No ATM breakeven at +10: {v[0][1].trigger_price}")
    h.run_to(37)                                    # 105.25 -> Target1
    v = h.views(t)
    check(v[0][2].status == "FILLED" and h.net() == 1, f"Target1 filled, runner remains: net={h.net()}")
    check(t.be_triggered and t.current_runner_stop == 102.25, "Auto-BE after Target1")
    h.run_to(81)                                    # first tick of bar H (index 8) -> trail scan
    v = h.views(t)
    check(v[1][1].trigger_price == 103.75, f"Swing trail: Stop2 at 103.75 (pivot 104.00 - 1 tick): {v[1][1].trigger_price}")
    h.run_to(95)
    st = h.eng.state()
    check(h.net() == 0 and not h.sn.trades and not st.open_orders, f"Runner stopped out, trade finished, open={st.open_orders}")
    check(abs(st.realized - 375.0) < 1e-6, f"Realized PnL {st.realized:.2f} (expected 375.00)")
    rows = h.strat.journal.rows
    check(len(rows) == 1 and rows[0]["Setup"] == "Smart Long WADES12" and rows[0]["Qty"] == "3"
          and rows[0]["Entry"] == "102,25" and rows[0]["PnL"] == "375",
          f"Journal: one row for the ATM trade: {rows}")
    tmp.cleanup()

    # ---------- 2b. Same path without auto-BE: trail still starts after Target1 (as in the original) ----------
    h = Harness(BAR_A + BAR_B + BAR_C + BAR_D + BAR_D2 + BAR_E + BAR_F + BAR_G + BAR_H + FLAT,
                SniperConfig(enable_auto_breakeven=False))
    h.run_to(22); h.sn.arm_smart(True); h.run_to(37)
    t = h.sn.trades[0]
    check(t.t1_filled and not t.be_triggered and h.views(t)[1][1].trigger_price == 99.75,
          f"No auto-BE: Stop2 stays at structural stop after Target1: {h.views(t)[1][1].trigger_price}")
    h.strat.move_order(t.brackets[1].sl, 106.00)    # sell stop above the market -> rejected
    h.run_to(38)
    check(h.views(t)[1][1].trigger_price == 99.75 and t.current_runner_stop == 99.75,
          f"Rejected manual move does not reach the Sniper: stop2 {t.current_runner_stop}")
    h.run_to(81)
    check(h.views(t)[1][1].trigger_price == 103.75, f"Trail without auto-BE: Stop2 at 103.75: {h.views(t)[1][1].trigger_price}")

    # ---------- 2c. Partial fill: exits follow the fill, rest cancelled after 10 ticks since the fill,
    #             filled contracts get a new OCO stop/target (the exchange cancels the old ones with the entry)
    fb = FakeBroker()
    sn = Sniper(fb, tick, SniperConfig())
    sn.cur = 5
    t = sn._place(True, load_atm_template("WADES12"), 100.50, 97.75, 102.25, None, "Smart Long")
    b1, b2 = t.brackets
    old_sl, old_tp = b1.sl, b1.tp

    def tick_at(px, high=None):
        sn.on_tick(RangeBar(px, max(px, high or px), px, px, 1, T0, T0), [bar(1, 1, 1, 1)] * 5, T0)

    fb.fill(b1.entry, 1, 100.50)
    tick_at(100.50, high=104.00)                    # bar high before the (pullback) fill must not count
    check(fb.orders[b1.sl]["qty"] == 1 and fb.orders[b1.tp]["qty"] == 1 and fb.orders[b1.sl]["trigger"] == 97.75,
          f"Partial fill: Stop1/Target1 follow the filled quantity: {fb.orders[b1.sl]}, {fb.orders[b1.tp]}")
    check(not t.entry_cancel_requested, "Bar extreme before the fill does not cancel the rest")
    for k in range(1, 10):
        tick_at(100.50 + k * tick)                  # +9 ticks since the fill
    check(not t.entry_cancel_requested, "9 ticks since the fill: rest still working")
    tick_at(103.00)                                 # +10 ticks
    check(t.entry_cancel_requested and fb.orders[b1.entry]["status"] == "CANCELED"
          and fb.orders[b2.entry]["status"] == "CANCELED", "10 ticks since the fill: rest of both entries cancelled")
    new_sl, new_tp = fb.orders[b1.sl], fb.orders[b1.tp]
    check(b1.sl != old_sl and new_sl["status"] == "ACCEPTED" and new_sl["qty"] == 1 and new_sl["trigger"] == 97.75
          and new_tp["qty"] == 1 and new_tp["price"] == 103.50,
          f"New OCO exits for the filled contract: stop {new_sl}, target {new_tp}")
    check(fb.orders[old_sl]["status"] == "CANCELED", "Old stop cancelled together with the entry")
    fb.fill(b1.tp, 1, 103.50)
    tick_at(103.50)
    check(not sn.trades and any("closed" in m for m in fb.logs), "Target of the replaced exits closes the trade")

    # ---------- 3. Smart Short: loss, trading continues (no daily lockout in the replay) ----------
    h = Harness(BAR_I0 + BAR_I + BAR_J + BAR_K + FLAT + FLAT)
    h.run_to(22)
    check(h.sn.arm_smart(False), "Smart Short armed")
    check(h.sn.trap and (h.sn.trap.trigger, h.sn.trap.limit, h.sn.trap.stop) == (100.75, 100.75, 103.50), f"Trap short: {h.sn.trap}")
    h.run_to(26)
    t = h.sn.trades[0] if h.sn.trades else None
    check(t is not None and t.entry_price == 100.75 and h.net() == -3, f"Short filled @ {t.entry_price if t else None}, net={h.net()}")
    h.run_to(30)
    v = h.views(t)
    check(v[0][1].trigger_price == 103.50, f"Stop1 short at 103.50: {v[0][1].trigger_price}")
    h.run_to(45)
    st = h.eng.state()
    check(h.net() == 0 and st.realized < 0, f"Stopped out, realized {st.realized:.2f}")
    h.run_to(52)
    h.sn.arm_smart(True)                            # flat bars: may be rejected by the signal bar filter, never locked
    check(not any("LOCKED" in e or "BLOCKED" in e for e in h.events()) and "BLOCKED" not in h.sn.notice,
          f"No trading lock after a losing trade (replay): notice {h.sn.notice!r}")

    # ---------- 4. Runway: pullback limit, price runs away -> cancel ----------
    BAR_A_LOW = [98.00, 98.25, 98.50, 99.00, 99.50, 100.00, 100.50, 100.75, 101.00, 100.75]
    RUN = [102.25, 102.50, 102.75, 103.00, 103.25, 103.50, 103.75, 104.00, 104.25, 104.50]
    h = Harness(BAR_A_LOW + BAR_B + [101.75, 101.75] + RUN[:8] + [104.75] * 10 + FLAT)
    h.run_to(22)
    check(h.sn.arm_smart(True) and h.sn.trap.limit == 100.50, f"Pullback limit 100.50: {h.sn.trap}")
    h.run_to(23)
    check(len(h.sn.trades) == 1 and len(h.open_orders()) == 2, f"Limit entries waiting: {h.open_orders()}")
    h.run_to(31)                                    # 104.75 = trigger + 10 ticks
    check(not h.sn.trades and not h.open_orders() and h.net() == 0, f"Runway cancel: open={h.open_orders()}")

    # ---------- 5. Bars to Wait: limit stays unfilled, 3 bars -> cancel ----------
    WAIT = [102.00, 102.25, 102.50, 102.25, 102.00, 102.25, 102.50, 102.25, 102.00, 102.25]
    h = Harness(BAR_A_LOW + BAR_B + [101.75, 101.75] + WAIT[:8] + WAIT + WAIT + WAIT + FLAT)
    h.run_to(22); h.sn.arm_smart(True); h.run_to(24)
    check(len(h.sn.trades) == 1, "Entry waiting (setup bar 2)")
    h.run_to(50)                                    # bar 4 forming, still allowed
    check(len(h.sn.trades) == 1 and len(h.open_orders()) == 2, "Still active after 2 bars")
    h.run_to(51)                                    # first tick of bar 5 = setup + 3
    check(not h.sn.trades and not h.open_orders(), "Bars-to-Wait cancel after 3 bars")

    # ---------- 6. Scratch: targets to entry + 2 ticks ----------
    h = Harness(BAR_A + BAR_B + BAR_C[:5] + [102.25, 102.50, 102.75, 102.75, 102.75] + FLAT)
    h.run_to(22); h.sn.arm_smart(True); h.run_to(26)
    t = h.sn.trades[0]
    check(t.entry_price == 102.25, "Scratch scenario: filled")
    check(h.sn.scratch() == 2, "Scratch: 2 targets pulled")
    h.run_to(27)                                    # modify takes effect with the next tick
    v = h.views(t)
    check(v[0][2].price == 102.75 and v[1][2].price == 102.75, f"Targets at 102.75: {v[0][2].price}, {v[1][2].price}")
    check(h.sn.scratch() == 0, "Second Scratch: nothing to do (only tighter)")
    h.run_to(32)
    st = h.eng.state()
    check(h.net() == 0 and abs(st.realized - 75.0) < 1e-6 and not h.sn.trades, f"Scratch exit realized {st.realized:.2f} (expected 75.00)")

    # ---------- 7. Cancel All: Trap and waiting entries ----------
    h = Harness(BAR_A_LOW + BAR_B + [101.75, 101.75] + WAIT[:8] + WAIT + FLAT)
    h.run_to(22); h.sn.arm_smart(True)
    h.sn.cancel_all()
    check(h.sn.trap is None, "Cancel All: Trap discarded")
    h.sn.arm_smart(True); h.run_to(24)
    check(len(h.open_orders()) == 2, "Entries in the book")
    h.sn.cancel_all(); h.run_to(26)
    check(not h.open_orders() and not h.sn.trades and h.net() == 0, "Cancel All: orders gone, flat")

    # ---------- 8. No-Runner variant and ATM switch ----------
    h = Harness(BAR_A + BAR_B + BAR_C + FLAT)
    h.run_to(22)
    check(h.sn.next_atm() == "WADES10" and h.sn.max_risk == 9, f"ATM switch: {h.sn.atm_name}, Risk {h.sn.max_risk}")
    check(h.sn.arm_smart(True, no_runner=True) and h.sn.trap.template.name == "WADES10NR", "No-Runner template selected")
    h.run_to(26)
    t = h.sn.trades[0]
    check(len(t.brackets) == 1 and t.brackets[0].quantity == 3 and h.net() == 3, f"NR: one bracket with 3, net={h.net()}")

    # ---------- 9. Momentum: market already beyond entry -> limit, otherwise stop-limit ----------
    h = Harness(BAR_A + BAR_B + [101.75, 101.75, 101.75, 102.00, 102.25, 102.25, 102.50, 102.75, 103.00, 103.25] + FLAT)
    h.run_to(22)
    check(h.sn.momentum(True), "Momentum Long submitted (stop-limit, market below entry)")
    t = h.sn.trades[0]
    check(all(v[0].trigger_price == 102.25 and v[0].price == 102.75 for v in h.views(t)), "Stop-Limit 102.25 / Limit 102.75")
    h.run_to(30)
    check(t.entry_price >= 102.25 and h.net() == 3, f"Momentum filled @ {t.entry_price}")
    v = h.views(t)
    check(v[0][1].trigger_price == 100.25, f"Momentum stop beyond signal bar (100.25): {v[0][1].trigger_price}")

    h.eng.end()
    print()
    if FAILS:
        print(f"{len(FAILS)} check(s) failed:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
