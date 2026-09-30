"""Command line of chart_app.py: argument parser and shell tab completion (argcomplete).

Kept free of heavy imports (Nautilus, Qt), so that a Tab press only loads this module.
Activation in the zsh: see README ("Tab completion").
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path.home() / "Desktop" / "trading" / "databento_data"   # location of the daily files for --day
BAR_WIDTH_PCT = 50                         # body width in percent of bar spacing (initial; changeable via field/--bar-width)
EMA_PERIOD = 21                            # EMA line on bar closes (0 = off)
SCHEMAS = ("trades", "mbo")
BAR_SUGGESTIONS = ("tick:2000", "range:4", "range:6", "range:8", "vol:2000")

_DAY_FILE = re.compile(r"glbx-mdp3-(\d{4})(\d{2})(\d{2})\.(trades|mbo)\.dbn\.zst$")
_QUARTER_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}


def available_days(data_dir: str | Path) -> dict[str, set[str]]:
    """Daily files in the folder: {'YYYY-MM-DD': {'trades', 'mbo'}}."""
    days: dict[str, set[str]] = {}
    try:
        names = os.listdir(data_dir)
    except OSError:
        return days
    for name in names:
        m = _DAY_FILE.match(name)
        if m:
            days.setdefault("-".join(m.groups()[:3]), set()).add(m.group(4))
    return days


def resolve_schema(day: str, data_dir: str | Path, schema: str | None) -> str:
    """Schema for --day: the given one, else 'trades' if that file exists, else 'mbo' if that one does."""
    if schema:
        return schema
    found = available_days(data_dir).get(day, set())
    return "trades" if "trades" in found or not found else "mbo"


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def es_contracts_for(day: date) -> list[str]:
    """Front ES contract on this day and the next one (CME roll: 8 days before the third Friday)."""
    y, m = day.year, day.month
    q = ((m - 1) // 3 + 1) * 3                  # quarterly month of this quarter
    if day >= _third_friday(y, q) - timedelta(days=8):
        q += 3
    out = []
    for k in range(2):
        qm, qy = q + 3 * k, y
        while qm > 12:
            qm -= 12
            qy += 1
        out.append(f"ES{_QUARTER_CODES[qm]}{qy % 10}")
    return out


def _data_dir(parsed_args) -> str:
    return getattr(parsed_args, "data_dir", None) or str(DATA_DIR)


def complete_day(prefix, parsed_args, **kwargs):
    schema = getattr(parsed_args, "schema", None)
    days = available_days(_data_dir(parsed_args))
    return sorted(d for d, schemas in days.items() if d.startswith(prefix) and (not schema or schema in schemas))


def complete_schema(prefix, parsed_args, **kwargs):
    days = available_days(_data_dir(parsed_args))
    wanted = getattr(parsed_args, "day", None) or list(days)
    found = set().union(*(days.get(d, set()) for d in wanted)) if wanted else set()
    return [s for s in SCHEMAS if s in found and s.startswith(prefix)] or [s for s in SCHEMAS if s.startswith(prefix)]


def complete_symbol(prefix, parsed_args, **kwargs):
    """Front and next ES contract of the chosen days (from the date only, the files are not opened)."""
    days = list(getattr(parsed_args, "day", None) or [])
    for path in getattr(parsed_args, "trades", None) or []:
        m = _DAY_FILE.search(Path(path).name)
        if m:
            days.append("-".join(m.groups()[:3]))
    if not days:
        days = list(available_days(_data_dir(parsed_args)))
    symbols: list[str] = []
    for d in sorted(days):
        try:
            contracts = es_contracts_for(date.fromisoformat(d))
        except ValueError:
            continue
        symbols += [c for c in contracts if c not in symbols]
    return [s for s in symbols if s.startswith(prefix.upper())]


def complete_atm(prefix, parsed_args, **kwargs):
    """ATM template names; completes the last entry of a comma-separated list."""
    from atm_templates import FALLBACK, NT_ATM_DIR
    names = set(FALLBACK)
    try:
        names |= {p.stem for p in NT_ATM_DIR.glob("*.xml")}
    except OSError:
        pass
    head, _, last = prefix.rpartition(",")
    head = head + "," if head else ""
    used = set(head.split(","))
    return [head + n for n in sorted(names) if n.startswith(last.upper()) and n not in used]


def complete_bars(prefix, parsed_args, **kwargs):
    return [b for b in BAR_SUGGESTIONS if b.startswith(prefix)]


def build_parser() -> argparse.ArgumentParser:
    try:
        from argcomplete.completers import DirectoriesCompleter, FilesCompleter
    except ImportError:                         # completion is optional; the parser works without it
        FilesCompleter = DirectoriesCompleter = lambda *a, **k: None
    ap = argparse.ArgumentParser(description="Charttrader: tick replay on NautilusTrader")
    dbn_files = FilesCompleter(("dbn.zst", "dbn"))
    ap.add_argument("trades", nargs="*", help="DBN file(s) with schema 'trades' or 'mbo'; empty = synthetic ticks"
                    ).completer = dbn_files
    ap.add_argument("--day", nargs="+", metavar="YYYY-MM-DD",
                    help="Trading day(s) as date; file glbx-mdp3-YYYYMMDD.<schema>.dbn.zst from --data-dir"
                    ).completer = complete_day
    ap.add_argument("--schema", default=None, choices=SCHEMAS,
                    help="File schema for --day (default: trades if that file exists, else mbo; "
                         "mbo = fills as ticks like NinjaTrader)").completer = complete_schema
    ap.add_argument("--data-dir", default=str(DATA_DIR), help=f"Folder of the daily files (default {DATA_DIR})"
                    ).completer = DirectoriesCompleter()
    ap.add_argument("--definition", help="DBN definition file (instrument incl. multiplier)").completer = dbn_files
    ap.add_argument("--symbol", help="Contract, e.g. ESH6 (default: most ticks)").completer = complete_symbol
    ap.add_argument("--bars", default="tick:2000",
                    help="Bar type: range:N (ticks), tick:N (Databento trade records per bar) or vol:N (contracts per bar); "
                         "default tick:2000. Note: Databento records are not NinjaTrader ticks (see README)"
                    ).completer = complete_bars
    ap.add_argument("--atm", default=None,
                    help="ATM templates, comma-separated (default: WADES12,WADES10,WADES14,WADES16,WADES8,WADES6)"
                    ).completer = complete_atm
    ap.add_argument("--all-buttons", action="store_true", help="Also show Deep and Momentum buttons")
    ap.add_argument("--bar-width", type=float, default=BAR_WIDTH_PCT, help="Body width in %% of bar spacing (default 50)")
    ap.add_argument("--ema", type=int, default=EMA_PERIOD, help="EMA period on bar closes, 0 = off (default 21)")
    ap.add_argument("--context", help="DBN file of the previous day, only as candle context (automatic with --day)"
                    ).completer = dbn_files
    ap.add_argument("--list", action="store_true", help="Show contracts per file and exit")
    ap.add_argument("--journal", default="journal/trade_log.csv",
                    help="trade log CSV, one row per closed trade (appended; 'off' = none; never for synthetic data)"
                    ).completer = FilesCompleter(("csv",))
    return ap


def autocomplete() -> None:
    """Answer a Tab press of the shell and exit; returns immediately on a normal start."""
    if "_ARGCOMPLETE" not in os.environ:
        return
    import argcomplete
    argcomplete.autocomplete(build_parser())
