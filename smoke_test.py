"""Headless smoke test for the ReplayEngine (no Qt).

Checks against Nautilus' SimulatedExchange with synthetic ticks:
market entry, limit target fill, bracket/OCO, flatten, jump + trade afterwards,
backward jump is ignored. Usage: python smoke_test.py
"""
from __future__ import annotations

import sys

from nautilus_trader.model.enums import OrderSide

from data_loader import es_contract, synthetic
from replay_engine import ReplayEngine

FAILS: list[str] = []


def check(cond: bool, msg: str):
    status = "OK  " if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        FAILS.append(msg)


def step_until(engine: ReplayEngine, pred, max_ticks: int, batch: int = 25) -> bool:
    """Play batches until pred(state) is true or max_ticks is reached."""
    played = 0
    while played < max_ticks and not engine.finished:
        engine.step(batch)
        played += batch
        if pred(engine.state()):
            return True
    return pred(engine.state())


def reachable_price(engine: ReplayEngine, above: bool, lookahead: int = 5000, ticks_away: int = 8) -> float:
    """A price several ticks away that actually trades within the next ticks."""
    window = engine.ticks[engine.i:engine.i + lookahead]
    prices = [float(t.price) for t in window]
    inc = float(engine.instrument.price_increment)
    last = engine.last_price
    if above:
        return min(max(prices) - inc, last + ticks_away * inc)
    return max(min(prices) + inc, last - ticks_away * inc)


def main() -> int:
    instrument = es_contract()
    ticks = synthetic(instrument, n=20_000)
    engine = ReplayEngine(instrument, ticks, enable_sniper=False)   # pure engine test
    strat = engine.strategy
    inc = float(instrument.price_increment)

    # --- 1. Warm-up ---
    engine.step(100)
    s = engine.state()
    check(s.net_qty == 0 and not s.open_orders, "Start flat, no orders")

    # --- 2. Market-Entry ---
    strat.market(OrderSide.BUY, 1)
    engine.step(25)
    s = engine.state()
    check(s.net_qty == 1, f"Market buy filled, net={s.net_qty}")
    check(any(e.startswith("FILL BUY") for e in strat.events), "Fill event BUY received")

    # --- 3. Limit target fill ---
    target = reachable_price(engine, above=True)
    strat.limit(OrderSide.SELL, 1, target)
    engine.step(1)                                   # one tick: order accepted but not filled yet
    s = engine.state()
    check(any(o[0] == "LIMIT" and o[1] == "SELL" for o in s.open_orders),
          f"Limit sell @ {target} rests in the book (last {s.last_price})")
    hit = step_until(engine, lambda st: st.net_qty == 0, max_ticks=8000)
    s = engine.state()
    check(hit, f"Limit target filled, net={s.net_qty}, realized={s.realized:.2f}")
    check(not s.open_orders, "Nothing left in the order book after target fill")

    # --- 4. Bracket + OCO ---
    realized_before = s.realized
    strat.bracket(OrderSide.BUY, 1, target_ticks=8, stop_ticks=8)
    engine.step(1)                                   # market fills on the next tick
    s = engine.state()
    check(s.net_qty == 1, f"Bracket entry filled, net={s.net_qty}")
    types = sorted(o[0] for o in s.open_orders)
    check(types == ["LIMIT", "STOP_MARKET"], f"Target + stop open: {types}")
    check(all(o[5] for o in s.open_orders), "Target + stop are marked as bracket legs (draggable)")
    sl = next(o for o in s.open_orders if o[0] == "STOP_MARKET")
    strat.move_order(sl[4], sl[3] - 2 * inc)
    engine.step(1)                                   # modify takes effect on the next tick
    moved = next(o for o in engine.state().open_orders if o[4] == sl[4])
    check(moved[3] == sl[3] - 2 * inc, f"Stop moved {sl[3]} -> {moved[3]}")
    strat.move_order(sl[4], engine.last_price + 2 * inc)   # sell stop above the market
    engine.step(1)
    kept = next(o for o in engine.state().open_orders if o[4] == sl[4])
    check(kept[3] == moved[3] and any(e.startswith("MODIFY REJECTED") for e in strat.events),
          f"Stop on the wrong side of the market rejected, stays at {kept[3]}")
    hit = step_until(engine, lambda st: st.net_qty == 0, max_ticks=5000)
    s = engine.state()
    check(hit, f"Bracket closed (target or stop), realized={s.realized:.2f}")
    check(not s.open_orders, f"OCO cancelled the opposite order, open={s.open_orders}")
    check(s.realized != realized_before, "Realized PnL changed by the bracket")

    # --- 5. Flatten ---
    strat.market(OrderSide.SELL, 2)
    engine.step(25)
    strat.limit(OrderSide.BUY, 1, engine.last_price - 20 * inc)  # far away, stays resting
    engine.step(25)
    s = engine.state()
    check(s.net_qty == -2 and len(s.open_orders) == 1, f"Before flatten: net={s.net_qty}, orders={len(s.open_orders)}")
    strat.flatten()
    engine.step(25)
    s = engine.state()
    check(s.net_qty == 0 and not s.open_orders, f"After flatten: net={s.net_qty}, orders={len(s.open_orders)}")

    # --- 6. Jump forward + trade afterwards ---
    before = engine.i
    engine.skip_to(before + 5000)
    check(engine.i == before + 5000, f"skip_to forward: i={engine.i}")
    engine.step(25)
    strat.market(OrderSide.SELL, 1)
    engine.step(25)
    s = engine.state()
    check(s.net_qty == -1, f"Trade after jump filled, net={s.net_qty}")
    strat.flatten()
    engine.step(25)
    check(engine.state().net_qty == 0, "Flat after post-jump trade")

    # --- 7. Backward jump is ignored ---
    here = engine.i
    engine.skip_to(here - 1000)
    check(engine.i == here, f"skip_to backward ignored: i={engine.i}")

    engine.end()
    print()
    print("Events:", *strat.events, sep="\n  ")
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
