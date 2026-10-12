"""Headless test of the trade log detail columns (signal bar, trap, targets, exits, manual changes).

Simulated trades against the SimulatedExchange with tick bars of 10 trades (paths from test_sniper.py):
1. Smart Long, direct trigger: Target1 + runner stopped at breakeven (auto-BE)
2. Smart Short, pullback limit: Target1 moved by hand, runner flattened by hand
3. Manual market entry without trap: detail columns stay empty
4. Smart Long trap expires without trigger: no-trade row with "Surprised" (box cleared)
5. Cancel All with an armed trap: no-trade row
plus: an existing log with the old short header is kept, the new rows go to trade_log_v2.csv.
Usage: python test_journal.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nautilus_trader.model.enums import OrderSide

from test_sniper import BAR_A, BAR_B, BAR_C, BAR_D, FAILS, Harness, check
from trade_journal import COLUMNS, COLUMNS_V1, ema_last

BAR_A_LOW = [98.00, 98.25, 98.50, 99.00, 99.50, 100.00, 100.50, 100.75, 101.00, 100.75]
BAR_BE = [105.00, 104.50, 104.00, 103.50, 103.00, 102.75, 102.50, 102.25, 102.00, 102.00]   # runner stop @ BE 102.25
FLAT_LOW = [102.00] * 10


def mirror(prices: list[float]) -> list[float]:
    """Price path mirrored at 100 (long setup -> short setup)."""
    return [200.0 - p for p in prices]


def last_line(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").splitlines()[-1]


def show(label: str, line: str, row: dict) -> None:
    print(f"\n{label}:")
    print("  " + line)
    print("  " + ", ".join(f"{k}={row[k]!r}" for k in COLUMNS[14:] if row[k]))


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    log = Path(tmp.name) / "trade_log.csv"
    log.write_text(";".join(COLUMNS_V1) + "\r\nold;row\r\n", encoding="utf-8-sig", newline="")   # old format
    samples = []

    # ---------- 1. Smart Long direct: T1 + runner on breakeven ----------
    h = Harness(BAR_A + BAR_B + BAR_C + BAR_D + BAR_BE + FLAT_LOW, journal=str(log))
    path = h.strat.journal.path
    check(path.name == "trade_log_v2.csv" and log.read_bytes().endswith(b"old;row\r\n"),
          f"Old short-header log kept, new log: {path.name}")
    check(path.read_text(encoding="utf-8-sig").splitlines()[0] == ";".join(COLUMNS), "New header in the v2 log")
    h.run_to(22)
    check(h.sn.arm_smart(True), "Smart Long armed (direct)")
    h.run_to(len(h.ticks))
    rows = h.strat.journal.rows
    check(len(rows) == 1, f"One row: {len(rows)}")
    r = rows[0]
    ema = ema_last([100.75, 101.75], 21)
    check(r["Entry"] == "102,25" and r["Setup"] == "Smart Long WADES12", f"Old columns unchanged: {r['Entry']} {r['Setup']}")
    check((r["SignalO"], r["SignalH"], r["SignalL"], r["SignalC"]) == ("100,75", "102,00", "100,50", "101,75"),
          "Signal bar OHLC")
    check((r["PrevBarH"], r["PrevBarL"]) == ("101,00", "100,00"), "Previous bar H/L")
    start = datetime.fromtimestamp(h.ticks[10].ts_event / 1e9, tz=ZoneInfo("Europe/Berlin"))   # first tick of bar B
    check(r["SignalBarStart"] == f"{start:%H:%M:%S}", f"Signal bar start {r['SignalBarStart']}")
    check(r["Trigger"] == "102,25" and r["EntryType"] == "Direct" and r["LimitPrice"] == "", "Direct: trigger, no limit")
    check(r["InitialSL"] == "99,75" and r["RiskTicks"] == "10", f"Initial SL / risk: {r['InitialSL']} {r['RiskTicks']}")
    check(r["Template"] == "WADES12" and r["MaxRiskTicks"] == "11", "Template, max risk")
    check(r["T1Price"] == "105,25" and r["T2Price"] == "108,25", f"Targets {r['T1Price']} {r['T2Price']}")
    check(r["ExitT1"] == "105,25" and r["ExitT1Time"] != "", f"T1 exit {r['ExitT1']} {r['ExitT1Time']}")
    check(r["ExitRunner"] in ("102,25", "102,00") and r["RunnerExitReason"] == "BE",
          f"Runner exit {r['ExitRunner']} reason {r['RunnerExitReason']}")
    check(r["ManualChanges"] == "", "No manual changes")
    check(r["DistEMA"] == f"{101.75 - ema:.2f}".replace(".", ","), f"DistEMA {r['DistEMA']}")
    samples.append(("1. Smart Long Direct, T1 + Runner BE", last_line(path), r))
    h.eng.end()

    # ---------- 2. Smart Short pullback limit: T1 moved by hand, runner flattened ----------
    trigger_bar = [98.25, 98.25, 97.75, 98.00, 98.50, 99.00, 99.50, 99.50, 99.50, 99.25]   # fills @ 99.50
    after = [99.00, 98.50, 98.00, 97.50, 97.25, 97.00, 97.00, 97.00, 97.25, 97.50]           # T1 moved to 97.00
    h = Harness(mirror(BAR_A_LOW) + mirror(BAR_B) + trigger_bar + after + [97.50] * 10, journal=str(log))
    path = h.strat.journal.path
    h.run_to(22)
    check(h.sn.arm_smart(False), "Smart Short armed (pullback)")
    check(h.sn.trap.limit == 99.50 and h.sn.trap.trigger == 97.75, f"Trap short: {h.sn.trap}")
    h.run_to(30)
    t = h.sn.trades[0]
    check(h.net() == -3, f"Short filled @ {t.entry_price}, net={h.net()}")
    h.strat.move_order(t.brackets[0].tp, 97.00)
    h.run_to(37)
    check(h.net() == -1, f"Moved T1 filled, runner remains: net={h.net()}")
    h.strat.flatten()
    h.run_to(40)
    rows = h.strat.journal.rows
    check(len(rows) == 1 and h.net() == 0, f"One row, flat: {len(rows)}")
    r = rows[0]
    check(r["EntryType"] == "PullbackLimit" and r["LimitPrice"] == "99,50" and r["Trigger"] == "97,75",
          f"Pullback: limit {r['LimitPrice']}, trigger {r['Trigger']}")
    check(r["InitialSL"] == "102,25" and r["RiskTicks"] == "11", f"Initial SL / risk {r['InitialSL']} {r['RiskTicks']}")
    check(r["T1Price"] == "96,50" and r["T2Price"] == "93,50", f"Planned targets {r['T1Price']} {r['T2Price']}")
    check(r["ManualChanges"] == "T1 96,50->97,00", f"Manual change: {r['ManualChanges']!r}")
    check(r["ExitT1"] == "97,00" and r["RunnerExitReason"] == "Manual", f"T1 {r['ExitT1']}, reason {r['RunnerExitReason']}")
    samples.append(("2. Smart Short PullbackLimit, T1 moved by hand, runner flattened", last_line(path), r))
    h.eng.end()

    # ---------- 3. Manual market entry without trap ----------
    h = Harness(BAR_A + BAR_B + [101.75] * 10, journal=str(log))
    path = h.strat.journal.path
    h.run_to(12)
    h.strat.market(OrderSide.BUY, 1)
    h.run_to(15)
    h.strat.flatten()
    h.run_to(18)
    r = h.strat.journal.rows[0]
    check(r["Setup"] == "Manual Market" and r["EntryType"] == "Manual" and r["RunnerExitReason"] == "Manual",
          f"Manual trade: {r['Setup']} {r['EntryType']} {r['RunnerExitReason']}")
    check(all(r[k] == "" for k in ("SignalBarStart", "SignalO", "Trigger", "LimitPrice", "InitialSL", "RiskTicks",
                                   "Template", "T1Price", "ExitT1", "DistEMA")), "Trap columns empty")
    samples.append(("3. Manual Market without trap", last_line(path), r))
    h.eng.end()

    # ---------- 4. Smart Long trap expires without trigger ("Surprised" ticked) ----------
    h = Harness(BAR_A + BAR_B + [101.50] * 10 + [101.50] * 10, journal=str(log))
    path = h.strat.journal.path
    h.run_to(22)
    check(h.sn.arm_smart(True), "Smart Long armed (direct)")
    h.strat.surprised = True
    h.run_to(len(h.ticks))
    rows = h.strat.journal.rows
    check(len(rows) == 1, f"One no-trade row: {len(rows)}")
    r = rows[0]
    check(r["Setup"] == "Smart Long WADES12" and r["Side"] == "LONG" and r["Qty"] == "0",
          f"No trade: {r['Setup']} {r['Side']} qty {r['Qty']}")
    check(all(r[k] == "" for k in ("Entry", "Exit", "Points", "Ticks", "PnL", "Duration", "ExitT1", "ExitRunner")),
          "No entry/exit/PnL")
    check(r["Trigger"] == "102,25" and r["InitialSL"] == "99,75" and r["RiskTicks"] == "10" and r["SignalC"] == "101,75",
          f"Trap columns filled: {r['Trigger']} {r['InitialSL']} {r['RiskTicks']}")
    check(r["RunnerExitReason"] == "NoTrade (trap expired)", f"Reason {r['RunnerExitReason']!r}")
    check(r["Surprised"] == "yes" and not h.strat.surprised, "Surprised logged, box cleared")
    check(r["Open"] <= r["Close"], f"Armed {r['Open']}, ended {r['Close']}")
    samples.append(("4. Smart Long trap expired", last_line(path), r))
    h.eng.end()

    # ---------- 5. Cancel All with an armed trap ----------
    h = Harness(BAR_A + BAR_B + [101.50] * 10, journal=str(log))
    h.run_to(22)
    check(h.sn.arm_smart(True), "Smart Long armed (direct)")
    h.sn.cancel_all()
    rows = h.strat.journal.rows
    check(len(rows) == 1 and rows[0]["RunnerExitReason"] == "NoTrade (cancel all)" and rows[0]["Surprised"] == "no",
          f"Cancel All row: {[r['RunnerExitReason'] for r in rows]}")
    h.eng.end()

    for s in samples:
        show(*s)
    tmp.cleanup()
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
