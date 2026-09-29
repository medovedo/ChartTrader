"""Read NinjaTrader ATM templates (bracket structure).

An ATM template consists of one or more brackets: quantity, stop and target
distance in ticks, plus optionally NinjaTrader's own auto-breakeven
(stop to entry + plus once the profit reaches the trigger).
Read directly from `Documents/NinjaTrader 8/templates/AtmStrategy/<Name>.xml`;
if the file is missing, a fallback table for the WADES family applies.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

NT_ATM_DIR = Path.home() / "Documents" / "NinjaTrader 8" / "templates" / "AtmStrategy"


@dataclass(frozen=True)
class AtmBracket:
    quantity: int
    stop_ticks: int
    target_ticks: int
    be_trigger_ticks: int = 0   # NT stop strategy: auto-breakeven after this many ticks of profit (0 = off)
    be_plus_ticks: int = 0      # ticks above/below entry the stop is then moved to


@dataclass(frozen=True)
class AtmTemplate:
    name: str
    brackets: tuple[AtmBracket, ...]

    @property
    def entry_quantity(self) -> int:
        return sum(b.quantity for b in self.brackets)

    @property
    def has_runner(self) -> bool:
        """Bracket 1 = Target1/Stop1, every further one = runner (Target2/Stop2)."""
        return len(self.brackets) > 1


def _wades(target: int) -> AtmTemplate:
    return AtmTemplate(f"WADES{target}", (AtmBracket(2, 12, target, 10, 0), AtmBracket(1, 12, 24, 10, 0)))


def _wades_nr(target: int) -> AtmTemplate:
    return AtmTemplate(f"WADES{target}NR", (AtmBracket(3, 12, target, 10, 0),))


FALLBACK: dict[str, AtmTemplate] = {}
for _t in (6, 8, 10, 12, 14, 16):
    FALLBACK[f"WADES{_t}"] = _wades(_t)
    FALLBACK[f"WADES{_t}NR"] = _wades_nr(_t)


def max_risk_from_name(name: str, default: int) -> int:
    """As in the Sniper: first number in the template name minus 1 (WADES12 -> 11), else default."""
    m = re.search(r"\d+", name)
    return int(m.group()) - 1 if m else default


def load_atm_template(name: str, directory: Path | str | None = None) -> AtmTemplate:
    """Read the XML from the NT folder; if missing, use the fallback table; otherwise FileNotFoundError."""
    path = Path(directory or NT_ATM_DIR) / f"{name}.xml"
    if path.exists():
        return parse_atm_xml(name, path)
    if name in FALLBACK:
        return FALLBACK[name]
    raise FileNotFoundError(f"ATM template '{name}' not found ({path}) and not in the fallback table")


def parse_atm_xml(name: str, path: Path) -> AtmTemplate:
    root = ET.parse(path).getroot()
    brackets = []
    for b in root.iter("Bracket"):
        ss = b.find("StopStrategy")
        brackets.append(AtmBracket(
            quantity=int(b.findtext("Quantity", "0")),
            stop_ticks=int(b.findtext("StopLoss", "0")),
            target_ticks=int(b.findtext("Target", "0")),
            be_trigger_ticks=int(ss.findtext("AutoBreakEvenProfitTrigger", "0")) if ss is not None else 0,
            be_plus_ticks=int(ss.findtext("AutoBreakEvenPlus", "0")) if ss is not None else 0,
        ))
    if not brackets or any(b.quantity <= 0 or b.stop_ticks <= 0 or b.target_ticks <= 0 for b in brackets):
        raise ValueError(f"ATM template '{name}' has no usable brackets: {path}")
    return AtmTemplate(name, tuple(brackets))
