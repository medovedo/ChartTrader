#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Charttrader: range bar chart with manual replay on NautilusTrader.

Controls
  Space         Play / pause (real-time: 1x = tick timestamps in real time)
  Right / Left  Speed factor up / down (0.5x ... 100x); pauses > 3 s are shortened
  N             Single step (one range bar)
  Mouse wheel   more / fewer visible bars
  Ctrl+wheel    compress / stretch price axis (also Ctrl + vertical drag)
  R             Price axis back to auto
  H             Horizontal line at the mouse pointer with price label on/off (default on)
  Hover a bar   info box: time from-to, open/high/low/close, volume, size in ticks, duration
  T             Trend line: two left clicks = line (preview follows the mouse), then the
                mode is off again; right click/Esc cancels. Click selects a line, dragging moves
                it (at an endpoint: only the point), Shift+drag drags a copy, Ctrl+C copies
                the selected line, Del deletes it (without selection the last one), Shift+Del all
  A             Text: click the position, enter the text (Enter = OK, Shift+Enter = new line). Select, drag, Shift+drag,
                Ctrl+C (copy, text also to the clipboard) and Del like trend lines; double click edits
                (empty text deletes). A jump deletes all drawings (bar indices are rebuilt)
  Ctrl+B/S      Market Buy / Sell (quantity from field)
  Shift+B/S     Bracket Buy / Sell (target/stop in ticks from fields)
  Sniper (port of LotsenhofSniper, panel on the right):
  W / S         Smart Long / Short (trap: trigger 1 tick above/below the signal bar)
  Ctrl+Up/Down  Momentum Long / Short (stop-limit immediately)
  X             Cancel All (trap, entries, positions)
  C             Scratch: targets to break-even + offset
  Mouse clicks in the chart place no orders (only buttons and keys).
  Drag stop/target line: moves the order of a filled bracket (thick lines; effective on the next tick,
                a stop on the wrong side of the market is rejected and snaps back)
  F             Flatten (close everything, cancel orders)
  Jump to       choose date/time + time zone, 'Go' – chart shows the last
                bars before that time as context, replay starts there

Start:  python chart_app.py                                  -> synthetic data
        python chart_app.py TRADES.dbn.zst [TRADES2.dbn.zst ...]  -> Databento day(s)
        Options: --symbol ESH6  (contract, otherwise the one with the most ticks)
                  --definition DEF.dbn.zst  (instrument definition, otherwise ES standard contract)
                  --bars range:4 | tick:2000 | vol:2000  (vol = contracts per bar; matches
                  NinjaTrader's "2000 Tick", since Databento bundles ~3.5 contracts per record)
                  --list  (show contracts and exit)
                  --bar-width 50 (body width in % of bar spacing, also in the "Width %" field)
                  --ema 21 (period of the EMA line, 0 = off)
                  --day loads the previous day automatically as context (all candles before the jump target),
                  --context FILE does that for a file path
"""
from __future__ import annotations

import sys
import bisect
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from chart_cli import BAR_WIDTH_PCT, EMA_PERIOD, autocomplete, build_parser, resolve_schema
autocomplete()   # Tab completion of the shell: answers and exits before the heavy imports below

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from nautilus_trader.model.enums import OrderSide

from data_loader import load_databento, synthetic
from range_bars import RangeBar, make_aggregator
from replay_engine import ReplayEngine
from sniper import SniperConfig


SPEEDS = [0.5, 1, 2, 5, 10, 20, 50, 100]   # playback factors relative to data time
FRAME_MS = 40                              # playback timer interval
MAX_IDLE_NS = 3_000_000_000                # pauses without trades are shortened to 3 s of data time
MAX_TICKS_PER_FRAME = 5_000                # safeguard when the machine can't keep up at high factors
VISIBLE_BARS = 600                         # drawn bars (older ones stay in the aggregator, just not on screen)
VIEW_BARS = 125                            # width of the view window in bars
RIGHT_MARGIN = 0.10                        # newest bar sits this far (fraction of the window) from the right edge


class CandleItem(pg.GraphicsObject):
    """Draws range bars as candles; x = bar index (start + list position)."""

    def __init__(self):
        super().__init__()
        self.bars: list[RangeBar] = []
        self.start = 0
        self.half_width = BAR_WIDTH_PCT / 200.0
        self.picture = QtGui.QPicture()
        self.rect = QtCore.QRectF()

    def set_bars(self, bars: list[RangeBar], start: int = 0, width_pct: float | None = None):
        self.bars = bars
        self.start = start
        if width_pct is not None:
            self.half_width = width_pct / 200.0
        self._redraw()

    def _redraw(self):
        # Bounding rect from the data, not from QPicture.boundingRect(): that one is integer
        # and cuts off the wick tip at prices like 6403.75 (the scene clips to it).
        self.prepareGeometryChange()
        if self.bars:
            lo = min(b.low for b in self.bars); hi = max(b.high for b in self.bars)
            pad = (hi - lo) * 0.01 + 1e-9
            self.rect = QtCore.QRectF(self.start - 1, lo - pad, len(self.bars) + 2, hi - lo + 2 * pad)
        else:
            self.rect = QtCore.QRectF()
        self.picture = QtGui.QPicture()
        p = QtGui.QPainter(self.picture)
        w = self.half_width
        for i, b in enumerate(self.bars, start=self.start):
            up = b.close >= b.open
            col = QtGui.QColor("#26a69a") if up else QtGui.QColor("#ef5350")
            # Wick: a 1 px hairline was invisible on 4K next to the 17 px wide body.
            # Hence a rectangle that scales with the bar width (like NinjaTrader), plus
            # a 2 px line as a lower bound when zoomed far out.
            p.setPen(pg.mkPen(col, width=2))
            p.setBrush(pg.mkBrush(col))
            p.drawLine(QtCore.QPointF(i, b.low), QtCore.QPointF(i, b.high))
            p.setPen(QtCore.Qt.NoPen)
            p.drawRect(QtCore.QRectF(i - w * 0.3, b.low, 2 * w * 0.3, b.high - b.low))   # ~30 % of body width
            p.setPen(pg.mkPen(col, width=1))
            p.drawRect(QtCore.QRectF(i - w, b.open, 2 * w, b.close - b.open).normalized())
        p.end()
        self.informViewBoundsChanged()
        self.update()

    def paint(self, p, *args):
        p.setRenderHint(QtGui.QPainter.Antialiasing, False)   # crisp candles, no grey haze
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return self.rect


def atr_extend(values: list[float], bars, period: int) -> None:
    """Appends Wilder ATR values for new closed bars (true range; start = mean of the first period)."""
    n = len(values)
    for i in range(n, len(bars)):
        b = bars[i]
        tr = b.high - b.low
        if i > 0:
            pc = bars[i - 1].close
            tr = max(tr, abs(b.high - pc), abs(b.low - pc))
        if i < period - 1:
            values.append(float("nan"))
        elif i == period - 1:
            trs = []
            for j in range(period):
                bj = bars[j]
                t = bj.high - bj.low
                if j > 0:
                    pcj = bars[j - 1].close
                    t = max(t, abs(bj.high - pcj), abs(bj.low - pcj))
                trs.append(t)
            values.append(sum(trs) / period)
        else:
            values.append((values[-1] * (period - 1) + tr) / period)


def ema_extend(values: list[float], closes: list[float], period: int) -> None:
    """Appends EMA values for new closes (like NinjaTrader: start value = first close)."""
    k = 2.0 / (period + 1)
    for c in closes[len(values):]:
        values.append(c if not values else c * k + values[-1] * (1 - k))


class TimeAxis(pg.AxisItem):
    """Bottom axis: every `every` bars the bar's close time (time zone from the selection)."""

    def __init__(self, bar_ts, tz_name, label_indices, every: int = 10):
        super().__init__(orientation="bottom")
        self.bar_ts = bar_ts                # callable: bar index -> ts_close in ns or None
        self.tz_name = tz_name              # callable: current time zone name
        self.label_indices = label_indices  # callable: (lo, hi) -> bar indices with session number % every == 0
        self.every = every

    def tickValues(self, minVal, maxVal, size):
        majors = [float(i) for i in self.label_indices(minVal, maxVal)]
        return [(self.every, majors)]

    def tickStrings(self, values, scale, spacing):
        tz = ZoneInfo(self.tz_name())
        out = []
        for v in values:
            ts = self.bar_ts(int(round(v)))
            out.append(datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).astimezone(tz).strftime("%H:%M")
                       if ts else "")
        return out


class TrendLine:
    """Segment (x1,y1)-(x2,y2) in bar index/price plus dashed extension to the right."""
    COLOR, SELECTED = "#e0e0e0", "#ffd54f"

    def __init__(self, plot, x1: float, y1: float, x2: float, y2: float):
        self.plot = plot
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.selected = False
        self.seg = pg.PlotCurveItem(); self.ext = pg.PlotCurveItem()
        self.handles = pg.ScatterPlotItem(size=10, brush=pg.mkBrush(self.SELECTED), pen=pg.mkPen("#000"))
        for it in (self.seg, self.ext, self.handles):
            plot.addItem(it)
        self.set_selected(False)

    def set_selected(self, on: bool):
        self.selected = on
        col = self.SELECTED if on else self.COLOR
        self.seg.setPen(pg.mkPen(col, width=2))
        self.ext.setPen(pg.mkPen(col, width=1, style=QtCore.Qt.DashLine))
        self.handles.setData([self.x1, self.x2] if on else [], [self.y1, self.y2] if on else [])

    def update(self, right: float):
        self.seg.setData([self.x1, self.x2], [self.y1, self.y2])
        if self.selected:
            self.handles.setData([self.x1, self.x2], [self.y1, self.y2])
        xa, ya, xb, yb = self.ordered()
        if xb != xa and right > xb:
            self.ext.setData([xb, right], [yb, yb + (yb - ya) / (xb - xa) * (right - xb)])
        else:
            self.ext.setData([], [])

    def ordered(self):
        return (self.x1, self.y1, self.x2, self.y2) if self.x2 >= self.x1 else (self.x2, self.y2, self.x1, self.y1)

    # Drawing interface (shared with TextNote): coordinates for dragging, copy, remove
    def coords(self) -> tuple:
        return self.x1, self.y1, self.x2, self.y2

    def drag_to(self, orig: tuple, handle: str, dx: float, dy: float):
        x1, y1, x2, y2 = orig
        if handle == "p1":
            self.x1, self.y1 = x1 + dx, y1 + dy
        elif handle == "p2":
            self.x2, self.y2 = x2 + dx, y2 + dy
        else:
            self.x1, self.y1, self.x2, self.y2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy

    def clone(self, offset_y: float = 0.0) -> "TrendLine":
        return TrendLine(self.plot, self.x1, self.y1 + offset_y, self.x2, self.y2 + offset_y)

    def describe(self) -> str:
        return "Trend line"

    def remove(self):
        for it in (self.seg, self.ext, self.handles):
            self.plot.removeItem(it)

    def hit(self, scene_pos, right: float, tol_px: float = 8.0):
        """Hit test in pixels: 'p1'/'p2' at an endpoint, 'body' on the line or extension, else None."""
        vb = self.plot.plotItem.vb
        sp = lambda x, y: vb.mapViewToScene(QtCore.QPointF(x, y))
        px, py = scene_pos.x(), scene_pos.y()
        for name, (x, y) in (("p1", (self.x1, self.y1)), ("p2", (self.x2, self.y2))):
            q = sp(x, y)
            if abs(q.x() - px) <= tol_px + 2 and abs(q.y() - py) <= tol_px + 2:
                return name
        xa, ya, xb, yb = self.ordered()
        xe, ye = (right, yb + (yb - ya) / (xb - xa) * (right - xb)) if xb != xa and right > xb else (xb, yb)
        a, b = sp(xa, ya), sp(xe, ye)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - a.x()) * dx + (py - a.y()) * dy) / l2))
        cx, cy = a.x() + t * dx, a.y() + t * dy
        return "body" if ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5 <= tol_px else None


class TextNote:
    """Text at (bar index, price), left edge vertically centred on the point; fixed pixel size."""
    COLOR, SELECTED = "#e0e0e0", "#ffd54f"

    def __init__(self, plot, x: float, y: float, text: str):
        self.plot = plot
        self.x, self.y, self.text = x, y, text
        self.selected = False
        self.item = pg.TextItem(text, color=self.COLOR, anchor=(0, 0.5))
        self.item.setFont(QtGui.QFont("Segoe UI", 11))
        self.item.setZValue(500)
        plot.addItem(self.item)
        self.set_selected(False)

    def set_selected(self, on: bool):
        self.selected = on
        self.item.border = pg.mkPen(self.SELECTED, width=1) if on else pg.mkPen(None)
        self.item.setColor(self.SELECTED if on else self.COLOR)
        self.item.update()

    def set_text(self, text: str):
        self.text = text
        self.item.setText(text)
        self.set_selected(self.selected)     # setText resets the colour

    def update(self, right: float):
        self.item.setPos(self.x, self.y)

    def coords(self) -> tuple:
        return self.x, self.y

    def drag_to(self, orig: tuple, handle: str, dx: float, dy: float):
        self.x, self.y = orig[0] + dx, orig[1] + dy

    def clone(self, offset_y: float = 0.0) -> "TextNote":
        return TextNote(self.plot, self.x, self.y + offset_y, self.text)

    def describe(self) -> str:
        return f"Text '{self.text.splitlines()[0] if self.text else ''}'"

    def remove(self):
        self.plot.removeItem(self.item)

    def hit(self, scene_pos, right: float, tol_px: float = 3.0):
        r = self.item.sceneBoundingRect().adjusted(-tol_px, -tol_px, tol_px, tol_px)
        return "body" if r.contains(scene_pos) else None


class ChartViewBox(pg.ViewBox):
    """Mouse wheel = number of visible bars, Ctrl+wheel or Ctrl+vertical drag = compress price axis."""

    def __init__(self, on_x_zoom, on_y_scale, on_drag):
        super().__init__()
        self.on_x_zoom = on_x_zoom
        self.on_y_scale = on_y_scale
        self.on_drag = on_drag          # drag trend lines; True = consumed, else default (pan)

    def wheelEvent(self, ev, axis=None):
        steps = ev.delta() / 120.0
        if ev.modifiers() & QtCore.Qt.ControlModifier:
            self.on_y_scale(1.15 ** (-steps))      # wheel down = compress
        else:
            self.on_x_zoom(1.2 ** (-steps))        # wheel down = more bars
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        # Dragging on a trend line takes precedence, even with Ctrl held
        if self.on_drag(ev):
            return
        if ev.modifiers() & QtCore.Qt.ControlModifier:
            dy = ev.screenPos().y() - ev.lastScreenPos().y()
            self.on_y_scale(1.01 ** dy)            # drag down = compress
            ev.accept()
        else:
            super().mouseDragEvent(ev, axis)


class _EnterAccepts(QtCore.QObject):
    """Event filter for a text field in a dialog: Enter accepts the dialog, Shift+Enter inserts a line break."""

    def eventFilter(self, obj, ev):
        if (ev.type() == QtCore.QEvent.KeyPress and ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
                and not ev.modifiers() & QtCore.Qt.ShiftModifier):
            self.parent().accept()
            return True
        return False


class ChartTrader(QtWidgets.QMainWindow):
    def __init__(self, instrument, ticks, bars="range:4", ticks_per_step=25, sniper_config=None,
                 bar_width_pct: float = BAR_WIDTH_PCT, ema_period: int = EMA_PERIOD, context_ticks=None,
                 journal_path: str | None = None):
        super().__init__()
        self.inc = float(instrument.price_increment)
        self.engine = ReplayEngine(instrument, ticks, agg=make_aggregator(bars, self.inc),
                                   sniper_config=sniper_config, journal_path=journal_path)
        self.context_ticks = list(context_ticks or [])   # previous day: only for bars/EMA, not through the engine
        self.ema_period = ema_period
        self._ema: list[float] = []
        self._ema_agg = None
        self._atr: list[float] = []
        self._atr_agg = None
        self.setWindowTitle(f"Charttrader – {instrument.id} – {self.engine.agg.label}")
        self.ticks_per_step = ticks_per_step
        self.speed_idx = SPEEDS.index(1)
        self.sim_ns = self.engine.ts        # simulated clock in data time (ns)
        self._last_real = 0.0               # last timer call in seconds (perf_counter)
        self.markers: list[tuple[int, float, str]] = []
        self.view_bars = VIEW_BARS
        self.y_span: float | None = None    # None = automatic price axis, else fixed height in points

        # --- Layout ---------------------------------------------------------
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        lay = QtWidgets.QVBoxLayout(central)
        self.plot = pg.PlotWidget(viewBox=ChartViewBox(self.zoom_x, self.scale_y, self.on_line_drag),
                                  axisItems={"bottom": TimeAxis(self.bar_ts, lambda: self.jump_tz.currentText(),
                                                                self.label_indices)})
        self.plot.showGrid(x=False, y=True, alpha=0.2)
        self.candles = CandleItem(); self.plot.addItem(self.candles)
        self.ema_curve = pg.PlotCurveItem(pen=pg.mkPen("#ffa726", width=2)); self.plot.addItem(self.ema_curve)
        # Info bottom right (like CandleSizeTickCounter in NT): ticks to next candle, candle sizes, ATR
        self.info = pg.TextItem(anchor=(1, 1), color="#ddd", fill=pg.mkBrush(30, 30, 30, 160))
        self.info.setFont(QtGui.QFont("Segoe UI", 11))
        self.info.setZValue(1000)
        # Hover box for the bar under the mouse pointer (in the scene, position in pixels next to the pointer)
        self.bar_box = pg.TextItem(anchor=(0, 0), color="#e0e0e0", fill=pg.mkBrush(20, 20, 20, 225),
                                   border=pg.mkPen("#757575"))
        self.bar_box.setFont(QtGui.QFont("Consolas", 10))
        self.bar_box.setZValue(1001)
        self.bar_box.setVisible(False)
        self._hover_pos = None          # last mouse position in the chart (scene), None = outside
        self.plot.scene().addItem(self.info)     # directly in the scene, position in pixels (see _refresh_info)
        self.plot.scene().addItem(self.bar_box)
        self.scatter = pg.ScatterPlotItem(size=12); self.plot.addItem(self.scatter)
        self.last_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#ffd54f", style=QtCore.Qt.DashLine))
        self.plot.addItem(self.last_line)
        # Horizontal line at the mouse pointer (H), price snapped to the tick; ignores the mouse so that
        # clicks and drags reach the orders/drawings underneath
        self.cursor_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#9e9e9e", width=1, style=QtCore.Qt.DotLine),
                                           label="{value:.2f}",
                                           labelOpts={"position": 0.97, "color": "#e0e0e0",
                                                      "fill": pg.mkBrush(30, 30, 30, 200)})
        self.cursor_line.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        self.cursor_line.setAcceptHoverEvents(False)
        self.cursor_line.setZValue(900)
        self.cursor_line.setVisible(False)
        self.plot.addItem(self.cursor_line, ignoreBounds=True)
        self.cursor_on = True
        self.order_lines: list[pg.InfiniteLine] = []
        self.trap_lines: list[pg.InfiniteLine] = []
        self.day_lines: dict[int, pg.InfiniteLine] = {}    # bar index of day change -> vertical line
        self.num_items: dict[int, pg.TextItem] = {}        # bar index -> number label (every 10th bar)
        self.drawings: list[TrendLine | TextNote] = []
        self.selected: TrendLine | TextNote | None = None
        self.text_mode = False
        self._drag = None               # (line, handle, original coordinates, start point) while dragging
        self._draggable: list[tuple] = []   # bracket legs from the last refresh: (id, price)
        self._order_drag = None         # [id, original price, current price] while dragging an order line
        self.pending_moves: dict = {}   # id -> new price until the exchange applies it (next tick)
        self.draw_mode = False
        self.draw_start: tuple[float, float] | None = None
        self.preview = pg.PlotCurveItem(pen=pg.mkPen("#e0e0e0", width=1, style=QtCore.Qt.DashLine))
        self.plot.addItem(self.preview)
        row = QtWidgets.QHBoxLayout(); lay.addLayout(row, 1)
        row.addWidget(self.plot, 1)
        row.addWidget(self._build_sniper_panel(), 0)

        bar = QtWidgets.QHBoxLayout(); lay.addLayout(bar)
        self.qty = QtWidgets.QSpinBox(); self.qty.setRange(1, 50); self.qty.setValue(1)
        self.tgt = QtWidgets.QSpinBox(); self.tgt.setRange(1, 400); self.tgt.setValue(8)
        self.stp = QtWidgets.QSpinBox(); self.stp.setRange(1, 400); self.stp.setValue(8)
        self.width_box = QtWidgets.QSpinBox(); self.width_box.setRange(5, 100); self.width_box.setSingleStep(5)
        self.width_box.setValue(int(bar_width_pct)); self.width_box.setSuffix(" %")
        self.width_box.valueChanged.connect(lambda _: self.refresh())
        for label, w in (("Qty", self.qty), ("Target (Ticks)", self.tgt), ("Stop (Ticks)", self.stp),
                         ("Width", self.width_box)):
            bar.addWidget(QtWidgets.QLabel(label)); bar.addWidget(w)
        for text, fn in (("▶/❚❚ Space", self.toggle), ("Buy (Ctrl+B)", lambda: self.market(OrderSide.BUY)),
                         ("Sell (Ctrl+S)", lambda: self.market(OrderSide.SELL)),
                         ("Bracket Buy", lambda: self.bracket(OrderSide.BUY)),
                         ("Bracket Sell", lambda: self.bracket(OrderSide.SELL)),
                         ("Flatten (F)", self.flatten)):
            b = QtWidgets.QPushButton(text); b.clicked.connect(fn); bar.addWidget(b)
        self.draw_btn = QtWidgets.QPushButton("Trend line (T)"); self.draw_btn.setCheckable(True)
        self.draw_btn.clicked.connect(self.toggle_draw); bar.addWidget(self.draw_btn)
        self.text_btn = QtWidgets.QPushButton("Text (A)"); self.text_btn.setCheckable(True)
        self.text_btn.clicked.connect(self.toggle_text); bar.addWidget(self.text_btn)
        self.cursor_btn = QtWidgets.QPushButton("Crosshair (H)"); self.cursor_btn.setCheckable(True)
        self.cursor_btn.setChecked(True)
        self.cursor_btn.clicked.connect(self.toggle_cursor_line); bar.addWidget(self.cursor_btn)
        bar.addSpacing(20); bar.addWidget(QtWidgets.QLabel("Jump to"))
        self.jump_dt = QtWidgets.QDateTimeEdit(); self.jump_dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.jump_dt.setCalendarPopup(True)
        first = datetime.fromtimestamp(ticks[0].ts_event / 1e9, tz=timezone.utc)
        self.jump_dt.setDateTime(QtCore.QDateTime(QtCore.QDate(first.year, first.month, first.day), QtCore.QTime(15, 30)))
        self.jump_tz = QtWidgets.QComboBox(); self.jump_tz.addItems(["UTC", "America/New_York", "Europe/Berlin"])
        self.jump_tz.setCurrentText("Europe/Berlin")
        go = QtWidgets.QPushButton("Go"); go.clicked.connect(self.jump_clicked)
        for w in (self.jump_dt, self.jump_tz, go): bar.addWidget(w)
        self.status = QtWidgets.QLabel(); lay.addWidget(self.status)
        self.notice = QtWidgets.QLabel(); self.notice.setStyleSheet("color: #ff5252; font-weight: bold")
        self.notice.setWordWrap(True); lay.addWidget(self.notice)
        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(90)
        lay.addWidget(self.log)

        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self.step)
        self.plot.scene().sigMouseClicked.connect(self.on_click)
        self.plot.scene().sigMouseMoved.connect(self.on_move)
        self.plot.installEventFilter(self)   # mouse leaving the chart
        QtWidgets.QApplication.instance().installEventFilter(self)   # hotkeys wherever the focus is (see eventFilter)
        QtCore.QTimer.singleShot(0, self.plot.setFocus)   # at start the keyboard belongs to the chart, not the qty field
        self._feed_context(self.context_ticks)   # previous day as candles before the first replay bar
        self._play(self.ticks_per_step)  # create first bar
        self._sync_clock(); self._drain_events(); self.refresh()

    # --- Sniper panel (replica of the WPF buttons from LotsenhofSniper) --------------
    @property
    def agg(self):
        return self.engine.agg

    @property
    def sniper(self):
        return self.engine.sniper

    def _build_sniper_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget(); grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(4, 4, 4, 4); grid.setSpacing(3)
        sn = self.sniper

        def btn(text, color, fn, fg="white"):
            b = QtWidgets.QPushButton(text)
            b.setStyleSheet(f"background: {color}; color: {fg}; font-weight: bold; padding: 6px 10px")
            b.clicked.connect(lambda: self.sniper_action(fn)); return b

        self.atm_btn = btn(f"ATM: {sn.atm_name}", "#191970", sn.next_atm)
        grid.addWidget(self.atm_btn, 0, 0, 1, 2)
        self.entry_btns = [
            (btn("LONG (Smart)  W", "#006400", lambda: sn.arm_smart(True)), 1, 0),
            (btn("LONG (No Runner)", "#228b22", lambda: sn.arm_smart(True, True)), 1, 1),
            (btn("SHORT (Smart)  S", "#8b0000", lambda: sn.arm_smart(False)), 2, 0),
            (btn("SHORT (No Runner)", "#b22222", lambda: sn.arm_smart(False, True)), 2, 1),
        ]
        r = 3
        if not sn.cfg.show_only_smart_buttons:
            self.entry_btns += [
                (btn("DEEP LONG (Max PB)", "#2e8b57", lambda: sn.arm_deep(True)), 3, 0),
                (btn("DEEP SHORT (Max PB)", "#b22222", lambda: sn.arm_deep(False)), 3, 1),
                (btn("MOMENTUM LONG  Ctrl+↑", "#3cb371", lambda: sn.momentum(True)), 4, 0),
                (btn("MOMENTUM SHORT  Ctrl+↓", "#cd5c5c", lambda: sn.momentum(False)), 4, 1),
            ]
            r = 5
        for b, rr, cc in self.entry_btns:
            grid.addWidget(b, rr, cc)
        grid.addWidget(btn(f"SCRATCH TP (+{sn.cfg.scratch_offset_ticks})  C", "#ffd700", sn.scratch, fg="black"), r, 0, 1, 2)
        grid.addWidget(btn("CANCEL ALL  X", "#ff8c00", sn.cancel_all), r + 1, 0, 1, 2)
        grid.setRowStretch(r + 2, 1)
        panel.setFixedWidth(330)
        return panel

    def sniper_action(self, fn):
        """Sniper button/hotkey: execute, messages to the log, refresh the display."""
        fn()
        self._drain_events()
        self.refresh()

    # --- Playback -----------------------------------------------------------
    @property
    def speed(self) -> float:
        return SPEEDS[self.speed_idx]

    def toggle(self):
        if self.timer.isActive():
            self.timer.stop()
        else:
            self._sync_clock(); self._last_real = time.perf_counter(); self.timer.start(FRAME_MS)

    def _sync_clock(self):
        """Set the simulated clock to the last played tick (after N, jump, pause)."""
        self.sim_ns = self.engine.ts

    def _play(self, n: int):
        """Pushes n ticks through the engine; aggregator and sniper run in the strategy's tick handler."""
        self.engine.step(n)

    def step(self):
        """Timer call: advance the clock by elapsed real time * factor, play due ticks."""
        if self.engine.finished:
            self.timer.stop(); self.log.appendPlainText("End of data."); return
        now = time.perf_counter()
        self.sim_ns += int((now - self._last_real) * self.speed * 1e9)
        self._last_real = now
        next_ts = self.engine.ts_index[self.engine.i]
        if next_ts - self.sim_ns > MAX_IDLE_NS:
            # Trading pause, halt or session end: don't wait for minutes
            self.sim_ns = next_ts - MAX_IDLE_NS
        end = bisect.bisect_right(self.engine.ts_index, self.sim_ns)
        n = min(end - self.engine.i, MAX_TICKS_PER_FRAME)
        if n > 0:
            self._play(n)
            if n == MAX_TICKS_PER_FRAME:
                self._sync_clock()  # machine can't keep up: pull the clock along instead of building a backlog
        self._drain_events()
        self.refresh()

    def step_bar(self):
        """Tick by tick until exactly one bar has closed (batches would overshoot the bar)."""
        n = len(self.agg.bars)
        while not self.engine.finished and len(self.agg.bars) == n:
            self._play(1)
        self._sync_clock(); self._drain_events(); self.refresh()

    # --- Jump to ------------------------------------------------------------
    def jump_clicked(self):
        q = self.jump_dt.dateTime()
        naive = datetime(q.date().year(), q.date().month(), q.date().day(),
                         q.time().hour(), q.time().minute())
        dt = naive.replace(tzinfo=ZoneInfo(self.jump_tz.currentText()))
        self.jump_to(dt)

    def bar_ts(self, i: int):
        """Close time (ns) of the bar with index i for the time axis; None outside."""
        bars = self.agg.bars
        if 0 <= i < len(bars):
            return bars[i].ts_close
        if i == len(bars) and self.agg.current is not None:
            return self.agg.current.ts_close
        return None

    def _feed_context(self, ticks) -> None:
        """Send ticks only through the aggregator (candles and EMA), not through the engine."""
        agg = self.agg
        for t in ticks:
            agg.update(float(t.price), float(t.size), t.ts_event)

    def jump_to(self, dt: datetime):
        """Fast-forwards without engine processing; context candles = previous day (if loaded) + everything up to the target."""
        st = self.engine.state()
        if st.net_qty != 0 or st.open_orders:
            self.log.appendPlainText("Jumping only without position and open orders (press F first)."); return
        self.timer.stop()
        target_ns = int(dt.astimezone(timezone.utc).timestamp() * 1e9)
        idx = bisect.bisect_left(self.engine.ts_index, target_ns)
        if idx <= self.engine.i:
            self.log.appendPlainText("Time is before the current position – jumping back is not supported."); return
        if idx >= len(self.engine.ticks):
            self.log.appendPlainText("Time is after the end of the data."); return
        self.engine.reset_aggregator()
        self.markers.clear()
        self.remove_drawings(all_items=True)     # bar indices are rebuilt
        for d in (self.day_lines, self.num_items):
            for it in d.values():
                self.plot.removeItem(it)
            d.clear()
        self._feed_context(self.context_ticks)
        self._feed_context(self.engine.ticks[:idx])
        self.engine.skip_to(idx)
        self.log.appendPlainText(f"Jumped to {dt.strftime('%Y-%m-%d %H:%M %Z')} (tick {idx})")
        self._play(self.ticks_per_step)
        self._sync_clock(); self._drain_events(); self.refresh()

    # --- Orders -------------------------------------------------------------
    def market(self, side):
        self.engine.strategy.market(side, self.qty.value()); self._drain_events()

    def bracket(self, side):
        self.engine.strategy.bracket(side, self.qty.value(), self.tgt.value(), self.stp.value())

    def flatten(self):
        self.engine.strategy.flatten()

    # --- Drawings: trend lines and text ------------------------------------
    def toggle_draw(self, on: bool | None = None):
        self.draw_mode = (not self.draw_mode) if on is None else bool(on)
        if self.draw_mode:
            self.toggle_text(False)
        self.draw_start = None
        self.preview.setData([], [])
        self.draw_btn.setChecked(self.draw_mode)
        self._set_cursor()
        if self.draw_mode:
            self.log.appendPlainText("Trend line: click start and end (right click/Esc cancels)")

    def toggle_text(self, on: bool | None = None):
        self.text_mode = (not self.text_mode) if on is None else bool(on)
        if self.text_mode:
            self.toggle_draw(False)
        self.text_btn.setChecked(self.text_mode)
        self._set_cursor()
        if self.text_mode:
            self.log.appendPlainText("Text: click the position in the chart (right click/Esc cancels)")

    def _set_cursor(self):
        crosshair = self.draw_mode or self.text_mode
        self.plot.setCursor(QtCore.Qt.CrossCursor if crosshair else QtCore.Qt.ArrowCursor)

    def _ask_text(self, default: str = "") -> str | None:
        """Text input: Enter accepts, Shift+Enter starts a new line; None = cancelled."""
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Text")
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.addWidget(QtWidgets.QLabel("Text in the chart (Enter = OK, Shift+Enter = new line):"))
        edit = QtWidgets.QPlainTextEdit(default); lay.addWidget(edit)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject); lay.addWidget(buttons)
        edit.installEventFilter(_EnterAccepts(dlg))
        edit.moveCursor(QtGui.QTextCursor.End); edit.setFocus()
        return edit.toPlainText().strip() if dlg.exec() == QtWidgets.QDialog.Accepted else None

    def add_text(self, x: float, y: float, text: str) -> TextNote | None:
        if not text:
            return None
        note = TextNote(self.plot, x, y, text)
        self._add_drawing(note)
        self.log.appendPlainText(f"{note.describe()} at {y} (bar {self._bar_no(x)})")
        return note

    def edit_text(self, note: TextNote):
        text = self._ask_text(note.text)
        if text is None:
            return
        if text:
            note.set_text(text)
        else:                                   # emptied: delete
            note.remove(); self.drawings.remove(note); self.selected = None

    def _view_point(self, scene_pos) -> tuple[float, float]:
        """Scene -> (bar index rounded, price rounded to tick)."""
        pt = self.plot.plotItem.vb.mapSceneToView(scene_pos)
        return float(round(pt.x())), round(round(pt.y() / self.inc) * self.inc, 10)

    def toggle_cursor_line(self, on: bool | None = None):
        self.cursor_on = (not self.cursor_on) if on is None else bool(on)
        self.cursor_btn.setChecked(self.cursor_on)
        # Show/hide at once at the current pointer position, not only on the next mouse move
        self.on_move(self.plot.mapToScene(self.plot.mapFromGlobal(QtGui.QCursor.pos())))

    def _bar_under(self, scene_pos, tol_px: float = 3.0) -> int | None:
        """Index of the bar whose column (±0.45 bar) and high-low range (± tol_px) contain the pointer."""
        vb = self.plot.plotItem.vb
        pt = vb.mapSceneToView(scene_pos)
        i = int(round(pt.x()))
        bars = self.agg.bars
        n = len(bars) + (1 if self.agg.current is not None else 0)
        if not (0 <= i < n) or abs(pt.x() - i) > 0.45:
            return None
        b = bars[i] if i < len(bars) else self.agg.current
        tol = abs(vb.mapSceneToView(scene_pos + QtCore.QPointF(0, tol_px)).y() - pt.y())
        return i if b.low - tol <= pt.y() <= b.high + tol else None

    def _bar_no(self, x: float) -> int:
        """Session bar number as labelled in the chart (x = internal index); right of the running bar
        it is counted on from the last bar."""
        i = int(round(x))
        no = self.agg.number_of(i)
        if no is not None:
            return no
        n = len(self.agg.bars) + (1 if self.agg.current is not None else 0)
        last = self.agg.number_of(n - 1) if n else None
        return last + (i - (n - 1)) if last is not None and i >= n else i

    def _bar_text(self, i: int) -> str:
        agg = self.agg
        running = i >= len(agg.bars)
        b = agg.current if running else agg.bars[i]
        zone = ZoneInfo(self.jump_tz.currentText())
        t0 = datetime.fromtimestamp(b.ts_open / 1e9, tz=zone)
        t1 = datetime.fromtimestamp(b.ts_close / 1e9, tz=zone)
        secs = (b.ts_close - b.ts_open) / 1e9
        s = int(secs)
        if secs < 60:
            dur = f"{secs:.1f} s".replace(".", ",")         # short bars (e.g. range bars at the open)
        elif s < 3600:
            dur = f"{s // 60}:{s % 60:02d} min"
        else:
            dur = f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d} h"
        no = agg.number_of(i)
        head = f"Bar {no}" + (" (running)" if running else "") if no else ("running bar" if running else "Bar")
        return "\n".join([
            f"{head}   {t0:%Y-%m-%d}",
            f"Time     {t0:%H:%M:%S} – {t1:%H:%M:%S}",
            f"Open     {b.open:.2f}",
            f"High     {b.high:.2f}",
            f"Low      {b.low:.2f}",
            f"Close    {b.close:.2f}",
            f"Volume   {b.volume:,.0f}".replace(",", "."),
            f"Size     {round((b.high - b.low) / self.inc)} ticks",
            f"Duration {dur}",
        ])

    def _update_bar_box(self):
        """Show/refresh the hover box for the bar under the pointer (also during playback)."""
        pos = self._hover_pos
        i = self._bar_under(pos) if pos is not None else None
        if i is None:
            self.bar_box.setVisible(False); return
        self.bar_box.setText(self._bar_text(i))
        # Next to the pointer; flip to the other side at the right/bottom edge of the chart
        r = self.plot.plotItem.vb.sceneBoundingRect()
        br = self.bar_box.boundingRect()
        x = pos.x() + 16 if pos.x() + 16 + br.width() <= r.right() else pos.x() - 16 - br.width()
        y = pos.y() + 16 if pos.y() + 16 + br.height() <= r.bottom() else pos.y() - 16 - br.height()
        self.bar_box.setPos(x, y)
        self.bar_box.setVisible(True)

    def on_move(self, pos):
        inside = self.plot.plotItem.vb.sceneBoundingRect().contains(pos)
        self._hover_pos = pos if inside else None
        self._update_bar_box()
        if self.cursor_on and inside:
            self.cursor_line.setPos(self._view_point(pos)[1])
        self.cursor_line.setVisible(self.cursor_on and inside)
        if self.draw_mode and self.draw_start is not None and self.plot.sceneBoundingRect().contains(pos):
            x, y = self._view_point(pos)
            self.preview.setData([self.draw_start[0], x], [self.draw_start[1], y])

    def add_trendline(self, x1: float, y1: float, x2: float, y2: float) -> TrendLine | None:
        if (x1, y1) == (x2, y2):
            return None
        line = TrendLine(self.plot, x1, y1, x2, y2)
        self._add_drawing(line)
        self.log.appendPlainText(f"Trend line {y1} -> {y2} (bars {self._bar_no(x1)}..{self._bar_no(x2)})")
        return line

    def _add_drawing(self, d):
        self.drawings.append(d)
        self.select(d)
        self._update_drawings()

    def select(self, d):
        for it in self.drawings:
            it.set_selected(it is d)
        self.selected = d

    def copy_drawing(self, d, offset_y: float = 0.0):
        """Copy of a trend line or text (optionally offset by offset_y), the copy is selected."""
        new = d.clone(offset_y)
        self._add_drawing(new)
        return new

    def copy_selected(self):
        if self.selected is None:
            self.log.appendPlainText("Nothing selected (click a trend line or text first)."); return
        lo, hi = self.plot.plotItem.vb.viewRange()[1]
        off = -round(round((hi - lo) * 0.05 / self.inc) * self.inc, 10)   # 5 % of view height lower
        if isinstance(self.selected, TextNote):
            QtWidgets.QApplication.clipboard().setText(self.selected.text)   # also usable outside the app
        self.copy_drawing(self.selected, off)
        self.log.appendPlainText(f"{self.selected.describe()} copied (copy is selected, drag to move).")

    def remove_drawings(self, all_items: bool = False):
        """Del: selected drawing, else the last one; Shift+Del: all."""
        if all_items:
            victims = list(self.drawings)
        elif self.selected is not None:
            victims = [self.selected]
        else:
            victims = self.drawings[-1:]
        for d in victims:
            d.remove(); self.drawings.remove(d)
        self.selected = None

    def _update_drawings(self):
        right = self.plot.plotItem.vb.viewRange()[0][1]
        for d in self.drawings:
            d.update(right)

    def _hit_drawing(self, scene_pos):
        right = self.plot.plotItem.vb.viewRange()[0][1]
        for d in reversed(self.drawings):             # topmost (newest) first
            h = d.hit(scene_pos, right)
            if h:
                return d, h
        return None

    def _hit_order(self, scene_pos, tol_px: float = 6.0):
        """Bracket leg (stop/target of a filled entry) near the mouse -> (id, price), else None."""
        vb = self.plot.plotItem.vb
        x = vb.mapSceneToView(scene_pos).x()
        best = None
        for oid, px in self._draggable:
            d = abs(vb.mapViewToScene(QtCore.QPointF(x, px)).y() - scene_pos.y())
            if d <= tol_px and (best is None or d < best[0]):
                best = (d, oid, px)
        return best[1:] if best else None

    def _drag_order(self, ev) -> None:
        """Drag a stop/target line; on release the order is modified (effective on the next tick)."""
        oid, px0, _ = self._order_drag
        vb = self.plot.plotItem.vb
        dy = vb.mapSceneToView(ev.scenePos()).y() - vb.mapSceneToView(ev.buttonDownScenePos()).y()
        self._order_drag[2] = round(px0 + round(dy / self.inc) * self.inc, 10)
        if ev.isFinish():
            price = self._order_drag[2]
            self._order_drag = None
            if price != px0:
                self.engine.strategy.move_order(oid, price)
                self.pending_moves[oid] = price
                self.log.appendPlainText(f"Order moved {px0:.2f} -> {price:.2f} (effective on the next tick)")
        self.refresh()

    def on_line_drag(self, ev) -> bool:
        """Drag a stop/target line or a trend line/an endpoint (Shift: drag a copy). False = no hit,
        ViewBox may pan. Also works in draw mode: clicks draw, dragging on a line moves it."""
        if ev.button() != QtCore.Qt.LeftButton:
            return False
        vb = self.plot.plotItem.vb
        if ev.isStart():
            hit = self._hit_order(ev.buttonDownScenePos())
            if hit is not None:
                self._order_drag = [hit[0], hit[1], hit[1]]
                ev.accept(); return True
        if self._order_drag is not None:
            self._drag_order(ev)
            ev.accept(); return True
        if ev.isStart():
            hit = self._hit_drawing(ev.buttonDownScenePos())
            if hit is None:
                return False
            d, handle = hit
            if ev.modifiers() & QtCore.Qt.ShiftModifier:
                d, handle = self.copy_drawing(d), "body"
            self.select(d)
            p0 = vb.mapSceneToView(ev.buttonDownScenePos())
            self._drag = (d, handle, d.coords(), (p0.x(), p0.y()))
            ev.accept(); return True
        if self._drag is None:
            return False
        d, handle, orig, (px0, py0) = self._drag
        p = vb.mapSceneToView(ev.scenePos())
        dx = float(round(p.x() - px0))
        dy = round(round((p.y() - py0) / self.inc) * self.inc, 10)
        d.drag_to(orig, handle, dx, dy)
        self._update_drawings()
        if ev.isFinish():
            self._drag = None
        ev.accept(); return True

    def on_click(self, ev):
        if not self.plot.sceneBoundingRect().contains(ev.scenePos()): return
        self.plot.setFocus()   # keyboard (Ctrl+C, Del, hotkeys) then belongs to the chart, not an input field
        if self.draw_mode:
            if ev.button() == QtCore.Qt.LeftButton:
                x, y = self._view_point(ev.scenePos())
                if self.draw_start is None:
                    self.draw_start = (x, y)
                else:
                    self.add_trendline(*self.draw_start, x, y)
                    self.toggle_draw(False)   # like NinjaTrader: back to normal mode after one line
            elif ev.button() == QtCore.Qt.RightButton:
                self.toggle_draw(False)
            return
        if self.text_mode:
            if ev.button() == QtCore.Qt.LeftButton:
                x, y = self._view_point(ev.scenePos())
                self.toggle_text(False)       # one text per activation, like the trend line
                text = self._ask_text()
                if text:
                    self.add_text(x, y, text)
            elif ev.button() == QtCore.Qt.RightButton:
                self.toggle_text(False)
            return
        # Outside draw mode, clicks in the chart deliberately trigger no orders:
        # trades only via buttons and hotkeys, so no misclick places an order.
        if ev.button() == QtCore.Qt.LeftButton:
            hit = self._hit_drawing(ev.scenePos())
            self.select(hit[0] if hit else None)
            if hit and ev.double() and isinstance(hit[0], TextNote):
                self.edit_text(hit[0])       # double click: edit the text (empty = delete)

    def eventFilter(self, obj, ev):
        # Hotkeys before the focus widget (application-wide filter): in the chart the ViewBox takes +/-
        # for its zoom history and the QGraphicsView the arrow keys for scrolling; a focused button
        # moves the focus on arrow keys and clicks on Space, a combo box switches its entry. Only
        # text inputs (quantity/ticks fields, date) keep their keys; what they don't use reaches
        # keyPressEvent. Dialogs and popups are other windows and are left alone.
        if (ev.type() == QtCore.QEvent.KeyPress and isinstance(obj, QtWidgets.QWidget)
                and obj.window() is self and not self._is_text_input(obj) and self._handle_key(ev)):
            return True
        if obj is self.plot and ev.type() == QtCore.QEvent.Leave:
            self.cursor_line.setVisible(False)      # mouse left the chart
            self._hover_pos = None
            self.bar_box.setVisible(False)
        return super().eventFilter(obj, ev)

    @staticmethod
    def _is_text_input(w) -> bool:
        if isinstance(w, (QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)):
            return not w.isReadOnly()        # the read-only log is no input
        return isinstance(w, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox))   # spin/date boxes and their line edit

    def keyPressEvent(self, e):
        if not self._handle_key(e):
            super().keyPressEvent(e)

    def _handle_key(self, e) -> bool:
        """Hotkeys; True = handled."""
        k, mod = e.key(), e.modifiers()
        shift = bool(mod & QtCore.Qt.ShiftModifier)
        ctrl = bool(mod & QtCore.Qt.ControlModifier)
        sn = self.sniper
        if k == QtCore.Qt.Key_Space: self.toggle()
        elif k == QtCore.Qt.Key_N: self.step_bar()
        elif k == QtCore.Qt.Key_B and shift: self.bracket(OrderSide.BUY)
        elif k == QtCore.Qt.Key_B and ctrl: self.market(OrderSide.BUY)
        elif k == QtCore.Qt.Key_S and shift: self.bracket(OrderSide.SELL)
        elif k == QtCore.Qt.Key_S and ctrl: self.market(OrderSide.SELL)
        elif k == QtCore.Qt.Key_F: self.flatten()
        elif k == QtCore.Qt.Key_R: self.reset_y()
        elif k == QtCore.Qt.Key_T: self.toggle_draw()
        elif k == QtCore.Qt.Key_A and not ctrl: self.toggle_text()
        elif k == QtCore.Qt.Key_H: self.toggle_cursor_line()
        elif k == QtCore.Qt.Key_Escape and (self.draw_mode or self.text_mode):
            self.toggle_draw(False); self.toggle_text(False)
        elif k == QtCore.Qt.Key_Delete: self.remove_drawings(all_items=shift)
        elif k == QtCore.Qt.Key_C and ctrl: self.copy_selected()
        elif sn and k == QtCore.Qt.Key_W and not ctrl: self.sniper_action(lambda: sn.arm_smart(True))
        elif sn and k == QtCore.Qt.Key_S: self.sniper_action(lambda: sn.arm_smart(False))
        elif sn and k == QtCore.Qt.Key_Up and ctrl: self.sniper_action(lambda: sn.momentum(True))
        elif sn and k == QtCore.Qt.Key_Down and ctrl: self.sniper_action(lambda: sn.momentum(False))
        elif sn and k == QtCore.Qt.Key_X: self.sniper_action(sn.cancel_all)
        elif sn and k == QtCore.Qt.Key_C and not ctrl: self.sniper_action(sn.scratch)
        elif k == QtCore.Qt.Key_Right and not (ctrl or shift):
            self.speed_idx = min(len(SPEEDS) - 1, self.speed_idx + 1)
        elif k == QtCore.Qt.Key_Left and not (ctrl or shift):
            self.speed_idx = max(0, self.speed_idx - 1)
        else:
            return False
        self.refresh()
        return True

    # --- Display ------------------------------------------------------------
    def _drain_events(self):
        ev = self.engine.strategy.events
        while ev:
            msg = ev.pop(0); self.log.appendPlainText(msg)
            if msg.startswith("FILL"):
                _, side, _, _, px = msg.split()
                self.markers.append((len(self.agg.bars), float(px), side))
            elif msg.startswith("MODIFY REJECTED"):
                self.pending_moves.clear()     # line snaps back to the price the exchange still holds

    # --- Zoom / scaling -----------------------------------------------------
    def zoom_x(self, factor: float):
        self.view_bars = int(min(VISIBLE_BARS, max(20, self.view_bars * factor)))
        self.refresh()

    def scale_y(self, factor: float):
        """Compress (>1) or stretch (<1) the price axis by factor; center stays, auto-range off."""
        vb = self.plot.plotItem.vb
        lo, hi = vb.viewRange()[1]
        self.y_span = max(4 * self.inc, (hi - lo) * factor)
        mid = (lo + hi) / 2
        vb.enableAutoRange(axis="y", enable=False)
        vb.setYRange(mid - self.y_span / 2, mid + self.y_span / 2, padding=0)

    def reset_y(self):
        self.y_span = None
        self.plot.plotItem.vb.enableAutoRange(axis="y", enable=True)

    def _follow_price(self):
        """With fixed height: pull the view along once the last price reaches the 15 % margin."""
        if self.y_span is None:
            return
        vb = self.plot.plotItem.vb
        lo, hi = vb.viewRange()[1]
        px = self.engine.last_price
        band = 0.15 * (hi - lo)
        if not (lo + band <= px <= hi - band):
            vb.setYRange(px - self.y_span / 2, px + self.y_span / 2, padding=0)

    def label_indices(self, lo: float, hi: float) -> list[int]:
        """Absolute bar indices in the range whose session number is a multiple of 10."""
        agg = self.agg
        n = len(agg.bars) + (1 if agg.current is not None else 0)
        out = []
        for i in range(max(0, int(lo)), min(n, int(hi) + 2)):
            no = agg.number_of(i)
            if no and no % 10 == 0:
                out.append(i)
        return out

    def _refresh_marks(self, n: int, start: int) -> None:
        """Vertical lines at day changes and bar numbers below every 10th bar (visible range only)."""
        agg, plot = self.agg, self.plot
        # Day change: line between the last bar of the previous day and the first bar of the new day
        for idx in agg.day_breaks:
            if idx not in self.day_lines and idx >= start:
                ts = agg.bars[idx].ts_open if idx < len(agg.bars) else (agg.current.ts_open if agg.current else None)
                label = ""
                if ts:
                    tz = ZoneInfo(self.jump_tz.currentText())
                    label = datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).astimezone(tz).strftime("%d.%m. %H:%M")
                ln = pg.InfiniteLine(pos=idx - 0.5, angle=90, pen=pg.mkPen("#9e9e9e", width=1, style=QtCore.Qt.DashLine),
                                     label=label, labelOpts={"position": 0.97, "color": "#9e9e9e", "rotateAxis": (1, 0)})
                plot.addItem(ln); self.day_lines[idx] = ln
        for idx in [i for i in self.day_lines if i < start]:
            plot.removeItem(self.day_lines.pop(idx))
        # Numbers: every 10th bar of the session, below the low
        lo, hi = plot.plotItem.vb.viewRange()[0]
        wanted = set(self.label_indices(max(lo, start), hi))
        for idx in [i for i in self.num_items if i not in wanted]:
            plot.removeItem(self.num_items.pop(idx))
        for idx in wanted:
            bar = agg.bars[idx] if idx < len(agg.bars) else agg.current
            if bar is None:
                continue
            item = self.num_items.get(idx)
            if item is None:
                item = pg.TextItem(str(agg.number_of(idx)), color="#4dd0e1", anchor=(0.5, 0))
                item.setFont(QtGui.QFont("Segoe UI", 9))
                plot.addItem(item); self.num_items[idx] = item
            item.setPos(idx, bar.low)

    def _refresh_info(self) -> None:
        """Ticks to the next candle, size of the current and previous candle, ATR 14 (all in ticks)."""
        agg, cur, closed, inc = self.agg, self.agg.current, self.agg.bars, self.inc
        if hasattr(agg, "ticks_per_bar"):
            left = f"Next candle in {agg.ticks_per_bar - agg.count} trades"
        elif hasattr(agg, "contracts_per_bar"):
            left = f"Next candle in {int(agg.contracts_per_bar - agg.count)} contracts"
        else:
            used = round((cur.high - cur.low) / inc) if cur else 0
            left = f"Range left: {agg.range_ticks - used} ticks"
        size = lambda b: round((b.high - b.low) / inc) if b else None
        cur_s, prev_s = size(cur), size(closed[-1]) if closed else None
        if self._atr_agg is not agg or len(self._atr) > len(closed):
            self._atr, self._atr_agg = [], agg
        atr_extend(self._atr, closed, 14)
        atr = self._atr[-1] / inc if self._atr and self._atr[-1] == self._atr[-1] else None
        left = f"Bar {agg.current_no}  |  " + left
        lines = [left,
                 f"Current candle: {cur_s if cur_s is not None else '-'} ticks   Last candle: {prev_s if prev_s is not None else '-'} ticks",
                 f"ATR 14: {atr:.1f} ticks" if atr is not None else "ATR 14: (fewer than 14 candles yet)"]
        self.info.setText("\n".join(lines))
        r = self.plot.plotItem.vb.sceneBoundingRect()
        self.info.setPos(r.right() - 8, r.bottom() - 8)   # anchor (1,1): bottom right corner of the chart area

    def _refresh_ema(self, bars, start: int) -> None:
        """Extend the EMA over closed bars (cache), append the running bar provisionally."""
        if self.ema_period <= 0 or not bars:
            self.ema_curve.setData([], []); return
        closed = self.agg.bars
        if self._ema_agg is not self.agg or len(self._ema) > len(closed):
            self._ema, self._ema_agg = [], self.agg          # new aggregator (jump): discard cache
        ema_extend(self._ema, [b.close for b in closed], self.ema_period)
        values = list(self._ema)
        if self.agg.current is not None:
            ema_extend(values, [b.close for b in closed] + [self.agg.current.close], self.ema_period)
        self.ema_curve.setData(list(range(start, len(values))), values[start:])

    def refresh(self):
        bars = self.agg.bars + ([self.agg.current] if self.agg.current else [])
        n = len(bars); start = max(0, n - VISIBLE_BARS)
        self.candles.set_bars(bars[start:], start, self.width_box.value())
        self._refresh_ema(bars, start)
        self._refresh_info()
        right = (n - 1) + self.view_bars * RIGHT_MARGIN     # newest bar at 90 % of the width, also at startup
        self.plot.setXRange(right - self.view_bars, right, padding=0)
        self._follow_price()
        self._update_drawings()
        self._refresh_marks(n, start)     # after setXRange: needs the current view range
        self.last_line.setPos(self.engine.last_price)
        self.scatter.setData(
            [{"pos": (x, y), "brush": pg.mkBrush("#26a69a" if s == "BUY" else "#ef5350"),
              "symbol": "t1" if s == "BUY" else "t"} for x, y, s in self.markers])
        for ln in self.order_lines + self.trap_lines: self.plot.removeItem(ln)
        self.order_lines.clear(); self.trap_lines.clear()
        st = self.engine.state()
        sn = self.sniper
        if sn is not None:
            if sn.trap:
                for px, col, style, txt in ((sn.trap.trigger, "#ffa500", QtCore.Qt.DashLine, "TRIGGER"),
                                            (sn.trap.limit, "#1e90ff", QtCore.Qt.SolidLine, f"TRAP {sn.trap.label}")):
                    ln = pg.InfiniteLine(pos=px, angle=0, pen=pg.mkPen(col, width=2, style=style),
                                         label=txt, labelOpts={"position": 0.9, "color": col})
                    self.plot.addItem(ln); self.trap_lines.append(ln)
            self.atm_btn.setText(f"ATM: {sn.atm_name}  (Risk {sn.max_risk})")
            self.notice.setText(sn.notice.replace("\n", " "))
        # Pending moves: keep showing the new price until the exchange confirms it (or the order is gone)
        open_px = {oid: px for *_, px, oid, _ in st.open_orders}
        self.pending_moves = {oid: p for oid, p in self.pending_moves.items()
                              if oid in open_px and abs(open_px[oid] - p) > self.inc / 2}
        self._draggable = []
        for typ, side, qty, px, oid, is_leg in st.open_orders:
            if self._order_drag is not None and self._order_drag[0] == oid:
                px = self._order_drag[2]
            else:
                px = self.pending_moves.get(oid, px)
            if is_leg:
                self._draggable.append((oid, px))
            col = "#42a5f5" if typ == "LIMIT" else "#ab47bc"
            ln = pg.InfiniteLine(pos=px, angle=0, pen=pg.mkPen(col, width=2 if is_leg else 1),
                                 label=f"{typ} {side} {qty:g} @ {px:.2f}", labelOpts={"position": 0.02, "color": col})
            self.plot.addItem(ln); self.order_lines.append(ln)
        ts = datetime.fromtimestamp(st.ts / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.status.setText(
            f"{ts} UTC   Last {st.last_price:.2f}   Pos {st.net_qty:+g}   "
            f"Unreal {st.unrealized:+.2f}   Real {st.realized:+.2f}   "
            f"Speed {self.speed:g}x   Tick {self.engine.i}/{len(self.engine.ticks)}")
        self._update_bar_box()          # after the view update: bars move under a still pointer during playback

    def closeEvent(self, e):
        self.timer.stop()
        try: self.engine.end()
        except Exception: pass
        super().closeEvent(e)


def main():
    args = build_parser().parse_args()

    from data_loader import day_file, previous_day_files
    if args.day:
        args.schema = resolve_schema(args.day[0], args.data_dir, args.schema)
    for day in args.day or []:
        path = day_file(day, args.data_dir, args.schema)
        if not path.exists():
            sys.exit(f"No file for {day}: {path}")
        args.trades.append(str(path))

    if args.list:
        from data_loader import contracts_in
        for path in args.trades:
            print(path)
            for sym, n in contracts_in(path).items():
                print(f"  {sym:<20} {n:>10}")
        return
    context_ticks = []
    if args.trades:
        instrument, ticks = load_databento(args.trades, args.definition, symbol=args.symbol)
        print(f"{instrument.id}: {len(ticks)} ticks from {len(args.trades)} file(s)")
        ctx_paths = [args.context] if args.context else []
        if not ctx_paths:
            if args.day:
                ctx_paths = previous_day_files(min(args.day), args.data_dir, args.schema)
            else:
                # File path: derive date, schema and folder from the Databento file name
                import re
                first = Path(sorted(args.trades)[0])
                m = re.search(r"(\d{4})(\d{2})(\d{2})\.(trades|mbo)\.dbn", first.name)
                if m:
                    ctx_paths = previous_day_files("-".join(m.groups()[:3]), first.parent, m.group(4))
        if ctx_paths:
            try:
                _, context_ticks = load_databento(ctx_paths, args.definition, symbol=str(instrument.id))
                context_ticks = [t for t in context_ticks if t.ts_event < ticks[0].ts_event]
                names = ", ".join(Path(p).name for p in ctx_paths)
                print(f"Context (previous day): {len(context_ticks)} ticks from {names}")
            except ValueError as e:
                print(f"No previous-day context: {e}")
        else:
            print("No previous-day context: no file found (EMA starts with the first bar of the day)")
    else:
        from nautilus_trader.test_kit.providers import TestInstrumentProvider
        instrument = TestInstrumentProvider.es_future(2025, 12)
        ticks = synthetic(instrument)
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=True, background="#1e1e1e", foreground="#ddd")
    cfg = SniperConfig(show_only_smart_buttons=not args.all_buttons)
    if args.atm:
        cfg.atm_templates = tuple(a.strip() for a in args.atm.split(",") if a.strip())
    w = ChartTrader(instrument, ticks, bars=args.bars, sniper_config=cfg, bar_width_pct=args.bar_width,
                    ema_period=args.ema, context_ticks=context_ticks,
                    journal_path=args.journal if args.trades and args.journal != "off" else None)
    w.showMaximized()   # fixed 1600x850 didn't fit on 2560 px at 125 % Windows scaling
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
