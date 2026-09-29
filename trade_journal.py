"""Trade journal: one CSV row per closed position (from Nautilus' PositionClosed event).

In NETTING mode a position is closed when it is flat again, so a Sniper trade with Target1 and
runner is one row (exit = average of all exits). The cache only keeps the last closed position
(the position ID is reused), which is why the rows come from the events.

The file is appended to row by row (survives a crash) and is meant for Excel on a German system:
separator ';', decimal comma, UTF-8 with BOM. Times in Europe/Berlin.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

COLUMNS = ["Replayed", "Date", "Open", "Close", "Instrument", "Setup", "Side", "Qty",
           "Entry", "Exit", "Points", "Ticks", "PnL", "Duration"]


def _num(x: float, decimal: str) -> str:
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return (s if s not in ("", "-0") else "0").replace(".", decimal)


def _duration(ns: int) -> str:
    s = ns // 1_000_000_000
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class TradeJournal:
    def __init__(self, path: str | Path, tick_size: float, tz: str = "Europe/Berlin",
                 sep: str = ";", decimal: str = ","):
        self.path = Path(path)
        self.tick = tick_size
        self.zone = ZoneInfo(tz)
        self.sep, self.decimal = sep, decimal
        self.replayed = datetime.now().strftime("%Y-%m-%d %H:%M")   # identifies this replay run
        self.rows: list[dict] = []

    def record(self, event, setup: str) -> dict:
        """Append a row for a PositionClosed event; returns the row."""
        sign = 1 if event.entry.name == "BUY" else -1
        entry, exit_ = float(event.avg_px_open), float(event.avg_px_close)
        points = (exit_ - entry) * sign
        t0 = datetime.fromtimestamp(event.ts_opened / 1e9, tz=self.zone)
        t1 = datetime.fromtimestamp(event.ts_closed / 1e9, tz=self.zone)
        d = self.decimal
        row = {
            "Replayed": self.replayed,
            "Date": f"{t0:%Y-%m-%d}",
            "Open": f"{t0:%H:%M:%S}",
            "Close": f"{t1:%H:%M:%S}",
            "Instrument": str(event.instrument_id).split(".")[0],
            "Setup": setup,
            "Side": "LONG" if sign > 0 else "SHORT",
            "Qty": f"{float(event.peak_qty):g}",
            "Entry": _num(entry, d),
            "Exit": _num(round(exit_, 2), d),          # average over partial exits, e.g. 5957,67
            "Points": _num(round(points, 2), d),
            "Ticks": _num(round(points / self.tick, 2), d),
            "PnL": _num(round(float(event.realized_pnl), 2), d),
            "Duration": _duration(event.duration_ns),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            if new:
                f.write(self.sep.join(COLUMNS) + "\r\n")
            f.write(self.sep.join(row[c] for c in COLUMNS) + "\r\n")
        self.rows.append(row)
        return row
