"""Interactive replay engine built on NautilusTrader (streaming backtest).

Ticks are fed to the BacktestEngine in small batches; between batches the
user can submit orders. Fills, positions and PnL come entirely from
Nautilus' SimulatedExchange.

The bar aggregator and the Sniper (port of LotsenhofSniper) hook into the
strategy's tick handler and therefore run tick-accurately in the Nautilus stream.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import TradeTick
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import (AccountType, ContingencyType, OmsType, OrderSide, OrderType, TimeInForce,
                                         TriggerType)
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, TraderId, Venue
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.orders import LimitOrder, OrderList, StopMarketOrder
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from range_bars import RangeBarAggregator
from sniper import OrderView, Sniper, SniperConfig
from trade_journal import TradeJournal

# Nautilus 1.231 calls pd.Timestamp.utcnow() in BacktestEngine.run (deprecated in pandas 3); not our code
warnings.filterwarnings("ignore", message=r"Timestamp\.utcnow is deprecated", category=DeprecationWarning)


class ManualStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: str


class ManualStrategy(Strategy):
    """A strategy without logic of its own: mouthpiece for click orders and broker for the Sniper."""

    def __init__(self, config: ManualStrategyConfig):
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.events: list[str] = []
        self.agg = None                 # set by ReplayEngine
        self.sniper: Sniper | None = None
        self._manual_moves: set[ClientOrderId] = set()   # GUI moves waiting for exchange confirmation
        self.journal = None                 # TradeJournal, set by ReplayEngine (optional)
        self._order_tags: dict[ClientOrderId, str] = {}  # entry order -> setup name for the journal
        self._position_setup = "Manual"
        self.realized_closed = 0.0          # sum over all closed positions (the cache keeps only the last one)

    def on_start(self):
        self.instrument = self.cache.instrument(InstrumentId.from_str(self.config.instrument_id))
        self.subscribe_trade_ticks(self.instrument.id)

    def on_trade_tick(self, tick: TradeTick):
        if self.agg is not None:
            self.agg.update(float(tick.price), float(tick.size), tick.ts_event)
            if self.sniper is not None:
                self.sniper.on_tick(self.agg.current, self.agg.bars, tick.ts_event)

    def on_order_filled(self, event):
        self.events.append(f"FILL {event.order_side.name} {event.last_qty} @ {event.last_px}")

    def on_order_rejected(self, event):
        self.events.append(f"REJECTED {event.reason}")

    def on_order_modify_rejected(self, event):
        self._manual_moves.discard(event.client_order_id)
        self.events.append(f"MODIFY REJECTED {event.reason}")

    def on_order_updated(self, event):
        # Manual move confirmed by the exchange: only now tell the Sniper (a rejected move must not count)
        if event.client_order_id in self._manual_moves:
            self._manual_moves.discard(event.client_order_id)
            px = event.trigger_price if event.trigger_price is not None else event.price
            if self.sniper is not None and px is not None:
                self.sniper.on_manual_move(event.client_order_id, float(px))

    def on_position_opened(self, event):
        self._position_setup = self._setup_of(event.opening_order_id)

    def on_position_closed(self, event):
        self.realized_closed += float(event.realized_pnl)
        if self.journal is not None:
            r = self.journal.record(event, self._position_setup)
            self.events.append(f"JOURNAL {r['Side']} {r['Qty']} {r['Entry']} -> {r['Exit']}  "
                               f"{r['Ticks']} ticks  PnL {r['PnL']}  ({r['Setup']})")

    def _setup_of(self, oid: ClientOrderId) -> str:
        """Origin of the order that opened the position: GUI tag or Sniper setup + ATM template."""
        if oid in self._order_tags:
            return self._order_tags[oid]
        if self.sniper is not None:
            for t in self.sniper.trades:
                if any(br.entry == oid for br in t.brackets):
                    return f"{t.label} {t.template.name}"
        return "Manual"

    # --- Calls from the GUI --------------------------------------------------
    def market(self, side: OrderSide, qty: int):
        order = self.order_factory.market(
            instrument_id=self.instrument.id,
            order_side=side,
            quantity=Quantity.from_int(qty),
        )
        self._order_tags[order.client_order_id] = "Manual Market"
        self.submit_order(order)

    def limit(self, side: OrderSide, qty: int, price: float):
        order = self.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=side,
            quantity=Quantity.from_int(qty),
            price=self.instrument.make_price(price),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

    def stop_market(self, side: OrderSide, qty: int, price: float):
        order = self.order_factory.stop_market(
            instrument_id=self.instrument.id,
            order_side=side,
            quantity=Quantity.from_int(qty),
            trigger_price=self.instrument.make_price(price),
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

    def bracket(self, side: OrderSide, qty: int, target_ticks: int, stop_ticks: int):
        """Market entry with OCO-linked target (limit) and stop (stop-market)."""
        inc = float(self.instrument.price_increment)
        last = self.cache.trade_tick(self.instrument.id)
        px = float(last.price) if last else 0.0
        sign = 1 if side == OrderSide.BUY else -1
        orders = self.order_factory.bracket(
            instrument_id=self.instrument.id,
            order_side=side,
            quantity=Quantity.from_int(qty),
            tp_price=self.instrument.make_price(px + sign * target_ticks * inc),
            sl_trigger_price=self.instrument.make_price(px - sign * stop_ticks * inc),
            tp_post_only=False,
            time_in_force=TimeInForce.GTC,
        )
        entry = next(o for o in orders.orders if o.parent_order_id is None)
        self._order_tags[entry.client_order_id] = "Manual Bracket"
        self.submit_order_list(orders)

    def flatten(self):
        self.cancel_all_orders(self.instrument.id)
        self.close_all_positions(self.instrument.id)

    # --- Broker interface for the Sniper ---------------------------------------------
    def place_bracket(self, is_long: bool, qty: int, limit_price: float, stop_price: float,
                      target_price: float, trigger_price: float | None = None):
        """Entry limit (or stop-limit) with stop-market and target limit as an OTO/OUO bracket."""
        mp = self.instrument.make_price
        orders = self.order_factory.bracket(
            instrument_id=self.instrument.id,
            order_side=OrderSide.BUY if is_long else OrderSide.SELL,
            quantity=Quantity.from_int(qty),
            entry_order_type=OrderType.STOP_LIMIT if trigger_price is not None else OrderType.LIMIT,
            entry_price=mp(limit_price),
            entry_trigger_price=mp(trigger_price) if trigger_price is not None else None,
            sl_trigger_price=mp(stop_price),
            tp_price=mp(target_price),
            tp_post_only=False,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order_list(orders)
        entry = next(o for o in orders.orders if o.parent_order_id is None)
        sl = next(o for o in orders.orders if o.parent_order_id is not None and o.order_type == OrderType.STOP_MARKET)
        tp = next(o for o in orders.orders if o.parent_order_id is not None and o.order_type == OrderType.LIMIT)
        return entry.client_order_id, sl.client_order_id, tp.client_order_id

    def place_exits(self, is_long: bool, qty: int, stop_price: float, target_price: float):
        """Stop-market + target limit as an OCO pair (OUO, reduce-only) for an existing position,
        e.g. after the partially filled entry of a bracket was cancelled (which cancels its OTO children)."""
        f, mp = self.order_factory, self.instrument.make_price
        sl_id, tp_id = f.generate_client_order_id(), f.generate_client_order_id()
        list_id = f.generate_order_list_id()
        side = OrderSide.SELL if is_long else OrderSide.BUY
        common = dict(trader_id=self.trader_id, strategy_id=self.id, instrument_id=self.instrument.id,
                      order_side=side, quantity=Quantity.from_int(qty), time_in_force=TimeInForce.GTC,
                      reduce_only=True, contingency_type=ContingencyType.OUO, order_list_id=list_id)
        sl = StopMarketOrder(client_order_id=sl_id, trigger_price=mp(stop_price), trigger_type=TriggerType.DEFAULT,
                             linked_order_ids=[tp_id], init_id=UUID4(), ts_init=self.clock.timestamp_ns(), **common)
        tp = LimitOrder(client_order_id=tp_id, price=mp(target_price), post_only=False,
                        linked_order_ids=[sl_id], init_id=UUID4(), ts_init=self.clock.timestamp_ns(), **common)
        self.submit_order_list(OrderList(order_list_id=list_id, orders=[sl, tp]))
        return sl_id, tp_id

    def order_view(self, oid: ClientOrderId) -> OrderView:
        o = self.cache.order(oid)
        price = getattr(o, "price", None)
        trig = getattr(o, "trigger_price", None)
        return OrderView(
            status=o.status.name,
            filled_qty=float(o.filled_qty),
            leaves_qty=float(o.leaves_qty),
            avg_px=float(o.avg_px) if o.avg_px is not None else 0.0,
            price=float(price) if price is not None else 0.0,
            trigger_price=float(trig) if trig is not None else 0.0,
            is_open=o.is_open,
            is_closed=o.is_closed,
            quantity=float(o.quantity),
        )

    def modify(self, oid: ClientOrderId, price: float | None = None, trigger_price: float | None = None,
               quantity: int | None = None) -> None:
        o = self.cache.order(oid)
        if not o.is_open:
            return
        mp = self.instrument.make_price
        self.modify_order(
            o,
            quantity=Quantity.from_int(quantity) if quantity is not None else None,
            price=mp(price) if price is not None else None,
            trigger_price=mp(trigger_price) if trigger_price is not None else None,
        )

    def cancel(self, oid: ClientOrderId) -> None:
        o = self.cache.order(oid)
        if o.is_open:
            self.cancel_order(o)

    def close_all(self) -> None:
        self.flatten()

    def move_order(self, oid: ClientOrderId, price: float) -> None:
        """Move a working order from the GUI (limit: price, stop: trigger). Takes effect on the next tick."""
        o = self.cache.order(oid)
        if o is None or not o.is_open:
            return
        if o.order_type == OrderType.STOP_MARKET:
            self.modify(oid, trigger_price=price)
        else:
            self.modify(oid, price=price)
        self._manual_moves.add(oid)      # Sniper is notified in on_order_updated

    def log(self, msg: str) -> None:
        self.events.append(msg)


@dataclass
class ReplayState:
    ts: int
    last_price: float
    net_qty: float
    unrealized: float
    realized: float
    open_orders: list[tuple[str, str, float, float, ClientOrderId, bool]]  # (type, side, qty, price, id, bracket leg)


class ReplayEngine:
    def __init__(self, instrument: Instrument, ticks: list[TradeTick],
                 starting_balance: float = 50_000.0, agg=None,
                 sniper_config: SniperConfig | None = None, enable_sniper: bool = True,
                 journal_path: str | None = None):
        self.instrument = instrument
        self.ticks = ticks
        self.i = 0
        self.engine = BacktestEngine(BacktestEngineConfig(
            trader_id=TraderId("REPLAY-001"),
            logging=LoggingConfig(log_level="ERROR"),
        ))
        self.venue = Venue(instrument.venue.value)
        self.engine.add_venue(
            venue=self.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            starting_balances=[Money(starting_balance, USD)],
            trade_execution=True,   # limit orders fill when price trades through
        )
        self.engine.add_instrument(instrument)
        self.strategy = ManualStrategy(ManualStrategyConfig(instrument_id=str(instrument.id)))
        tick_size = float(instrument.price_increment)
        self.strategy.agg = agg if agg is not None else RangeBarAggregator(tick_size=tick_size, range_ticks=4)
        if enable_sniper:
            self.strategy.sniper = Sniper(self.strategy, tick_size, sniper_config)
        if journal_path:
            self.strategy.journal = TradeJournal(journal_path, tick_size)
        self.engine.add_strategy(self.strategy)
        self.last_price = float(ticks[0].price) if ticks else 0.0
        self.ts = ticks[0].ts_event if ticks else 0
        self.ts_index = [t.ts_event for t in ticks]  # for bisect when jumping

    @property
    def agg(self):
        return self.strategy.agg

    @property
    def sniper(self) -> Sniper | None:
        return self.strategy.sniper

    def reset_aggregator(self):
        """Install an empty aggregator of the same kind (before a jump); the trap is discarded."""
        self.strategy.agg = self.strategy.agg.fresh()
        if self.strategy.sniper is not None:
            self.strategy.sniper.trap = None
        return self.strategy.agg

    def skip_to(self, idx: int):
        """Advance the read pointer without sending the ticks through the engine.
        Only meaningful without open positions/orders (the exchange never sees the skipped prices)."""
        self.i = max(self.i, idx)
        self.last_price = float(self.ticks[idx - 1].price) if idx > 0 else self.last_price
        self.ts = self.ticks[idx - 1].ts_event if idx > 0 else self.ts

    # --- Playback --------------------------------------------------------------
    def step(self, n: int = 1) -> list[TradeTick]:
        """Play the next n ticks. Returns the ticks played."""
        batch = self.ticks[self.i:self.i + n]
        if not batch:
            return []
        self.i += len(batch)
        self.engine.add_data(batch)
        self.engine.run(streaming=True)
        self.engine.clear_data()
        self.last_price = float(batch[-1].price)
        self.ts = batch[-1].ts_event
        return batch

    @property
    def finished(self) -> bool:
        return self.i >= len(self.ticks)

    def end(self):
        self.engine.end()

    # --- State for the GUI -----------------------------------------------------
    def state(self) -> ReplayState:
        cache = self.engine.cache
        iid = self.instrument.id
        net = 0.0
        unreal = 0.0
        for pos in cache.positions_open(instrument_id=iid):
            net += float(pos.signed_qty)
            unreal += float(pos.unrealized_pnl(Price(self.last_price, self.instrument.price_precision)))
        # NETTING reuses the position ID, so positions_closed() only holds the last one: closed PnL comes from the events
        realized = self.strategy.realized_closed
        realized += sum(float(p.realized_pnl) for p in cache.positions_open(instrument_id=iid))
        orders = []
        for o in cache.orders_open(instrument_id=iid):
            px = getattr(o, "price", None) or getattr(o, "trigger_price", None)
            orders.append((o.order_type.name, o.side.name, float(o.leaves_qty), float(px) if px else 0.0,
                           o.client_order_id, o.parent_order_id is not None))
        return ReplayState(self.ts, self.last_price, net, unreal, realized, orders)
