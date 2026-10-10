"""Chart display of the EntryCount/DoubleTrap ports: draws their Draw.* objects (pa_support.Drawings) in pyqtgraph.

Like NinjaTrader: a text hangs down from its anchor (bar, price) and the pixel offset moves it up (positive)
or down (negative), so the label rows of PaLabelLayout keep their spacing at every zoom level.
Only the visible bars are drawn; texts are repositioned when the zoom changes.
"""
from __future__ import annotations

import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui

from pa_support import DrawDiamond, DrawDot, DrawLine, DrawText


def qcolor(name: str) -> QtGui.QColor:
    """WPF brush name -> QColor (the names are the SVG color names Qt knows)."""
    c = QtGui.QColor(name.lower())
    return c if c.isValid() else QtGui.QColor("#cccccc")


class PaOverlay:
    def __init__(self, plot: pg.PlotWidget):
        self.plot = plot
        self.items: dict[str, object] = {}       # tag -> graphics item
        self.objs: dict[str, object] = {}        # tag -> drawing object shown by that item
        self.markers = pg.ScatterPlotItem(pxMode=True)
        self.markers.setZValue(50)
        plot.addItem(self.markers, ignoreBounds=True)
        self._marker_key = None
        self._sources: tuple = ()
        self._last = None               # (drawings, first, last, visible) of the last update
        # zoom/pan without a refresh (paused, mouse drag): cover the newly visible bars, keep the pixel offsets
        plot.plotItem.vb.sigRangeChanged.connect(lambda *_: self._on_range_changed())

    def clear(self) -> None:
        for it in self.items.values():
            self.plot.removeItem(it)
        self.items.clear()
        self.objs.clear()
        self.markers.setData([])
        self._marker_key = None

    def _on_range_changed(self) -> None:
        if self._last is not None:
            self.update(*self._last)
        else:
            self.reposition()

    def update(self, drawings: list, first: int, last: int, visible=None) -> None:
        """drawings = Drawings objects to show; first..last = bars that have candles (inclusive), intersected with
        the current view; visible(tag, obj) -> bool filters by kind (display switches of the chart)."""
        self._last = (drawings, first, last, visible)
        vlo, vhi = self.plot.plotItem.vb.viewRange()[0]
        lo, hi = max(first, int(vlo) - 1), min(last, int(vhi) + 1)
        if tuple(id(d) for d in drawings) != self._sources:      # new runner after a jump
            self.clear()
            self._sources = tuple(id(d) for d in drawings)
        wanted: dict[str, object] = {}
        marks = []
        for d in drawings:
            for tag, o in d.objects.items():
                if visible is not None and not visible(tag, o):
                    continue
                if isinstance(o, DrawLine):
                    if o.bar2 >= lo and o.bar1 <= hi:
                        wanted[tag] = o
                elif lo <= o.bar <= hi:
                    if isinstance(o, (DrawDot, DrawDiamond)):
                        marks.append(o)
                    else:
                        wanted[tag] = o
        for tag in [t for t in self.items if t not in wanted]:
            self.plot.removeItem(self.items.pop(tag))
            self.objs.pop(tag, None)
        for tag, o in wanted.items():
            it = self.items.get(tag)
            if it is None or self.objs.get(tag) is not o:
                if it is not None:
                    self.plot.removeItem(it)
                it = self._make(o)
                self.plot.addItem(it, ignoreBounds=True)   # pixel offsets must not widen the price axis
                self.items[tag] = it
                self.objs[tag] = o
        self.reposition()
        key = tuple((id(m), m.bar) for m in marks)
        if key != self._marker_key:
            self._marker_key = key
            self.markers.setData([{"pos": (m.bar, m.y), "symbol": "d" if isinstance(m, DrawDiamond) else "o",
                                   "size": 11 if isinstance(m, DrawDiamond) else 7,
                                   "brush": pg.mkBrush(qcolor(m.color)), "pen": pg.mkPen(None)} for m in marks])

    def reposition(self) -> None:
        """Texts: anchor price + pixel offset converted with the current scale."""
        if not self.items:
            return
        ypx = self.plot.plotItem.vb.viewPixelSize()[1]
        for tag, it in self.items.items():
            o = self.objs[tag]
            if isinstance(o, DrawText):
                it.setPos(o.bar, o.y + o.offset_px * ypx)

    @staticmethod
    def _make(o):
        if isinstance(o, DrawText):
            it = pg.TextItem(o.text, color=qcolor(o.color), anchor=(0.5, 0))
            font = QtGui.QFont("Arial", o.font_size)
            font.setBold(o.bold)
            it.setFont(font)
            it.setZValue(60)
            return it
        pen = pg.mkPen(qcolor(o.color), width=o.width, style=QtCore.Qt.DashLine if o.dashed else QtCore.Qt.SolidLine)
        it = pg.PlotCurveItem([o.bar1, o.bar2], [o.y1, o.y2], pen=pen)
        it.setZValue(55)
        return it
