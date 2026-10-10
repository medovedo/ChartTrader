"""Parameters of the EntryCount and DoubleTrap ports.

Names and defaults are the NinjaScript properties of EntryCount.cs / DoubleTrap.cs (State.SetDefaults),
so a value can be carried over 1:1 in both directions. The defaults live here; `entrycount_doubletrap.toml`
overrides them without code changes (unknown keys are an error, so a typo cannot silently fall back to a default).
Enums are written as their C# member names, colors as WPF brush names.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("entrycount_doubletrap.toml")

OUTSIDE_BAR_POLICIES = ("CloseDirection", "BothSides", "Ignore")
STOP_MODES = ("SignalAndPrevBar", "SignalBarOnly")
RESET_SCOPES = ("CounterTrendOnly", "Both")
FAILED_ENTRY_MODES = ("PriorBarBreak", "SignalBarBreak")


@dataclass
class EntryCountConfig:
    """EntryCount.OnStateChange, State.SetDefaults (EntryCount.cs lines 664-738)."""
    # Shared
    EmaPeriod: int = 21
    KeyLevelToleranceTicks: int = 2
    SwingStrength: int = 2
    UseSwingSR: bool = False
    SrLookbackSwings: int = 5
    SrToleranceTicks: int = 2
    UseTrendlines: bool = False
    OutsideBarPolicy: str = "BothSides"
    EmaSlopeLookback: int = 5
    MinEmaSlopeTicks: float = 2.0
    # Signal bar (Rule 5)
    MinBodyPct: float = 45.0
    MinClosePct: float = 70.0
    MaxBreakoutWickPct: float = 25.0
    UseRejectionException: bool = True
    RejectionMinClosePct: float = 90.0
    RejectionMaxWickPct: float = 10.0
    # Stop / risk
    StopMode: str = "SignalAndPrevBar"
    RiskTemplateTicks: int = 16
    RiskToleranceTicks: int = 2
    MaxPullbackPct: float = 49.0
    # Counting / reset
    RequireEqualOrHigherLeg: bool = False
    ResetOnKeyLevel: bool = True
    ResetScope: str = "CounterTrendOnly"
    ResetAfterTwoLegs: bool = True
    ResetOnNewExtreme: bool = True
    FailedEntryMode: str = "PriorBarBreak"
    InsideBarAsSignal: bool = True
    # Display
    ShowLabels: bool = True
    MaxLabeledCount: int = 3
    ShowLongCount: bool = True
    ShowShortCount: bool = True
    ShowFirstEntries: bool = True
    ShowFailedEntries: bool = True
    ShowCountOrigin: bool = True
    ShowQualityColoring: bool = True
    ShowRiskTicks: bool = False
    LabelFontSize: int = 10
    BarNumberClearance: int = 32
    EnableAlerts: bool = False
    # Colors
    LongColor: str = "DodgerBlue"
    ShortColor: str = "Crimson"
    QualityFailColor: str = "Gray"
    FailedLongColor: str = "Green"
    FailedShortColor: str = "Red"
    OriginColor: str = "Goldenrod"
    # Diagnostics
    DebugLog: bool = False
    ExportCsv: bool = False
    CsvIncludeBars: bool = True


@dataclass
class DoubleTrapConfig:
    """DoubleTrap.OnStateChange, State.SetDefaults (DoubleTrap.cs lines 127-230)."""
    # Shared (passed to EntryCount)
    EmaPeriod: int = 21
    KeyLevelToleranceTicks: int = 2
    SwingStrength: int = 2
    UseSwingSR: bool = False
    SrLookbackSwings: int = 5
    SrToleranceTicks: int = 2
    UseTrendlines: bool = False
    OutsideBarPolicy: str = "BothSides"
    EmaSlopeLookback: int = 5
    MinEmaSlopeTicks: float = 2.0
    # Signal bar (Rule 5)
    MinBodyPct: float = 45.0
    MinClosePct: float = 70.0
    MaxBreakoutWickPct: float = 25.0
    UseRejectionException: bool = True
    RejectionMinClosePct: float = 90.0
    RejectionMaxWickPct: float = 10.0
    # Stop / risk
    StopMode: str = "SignalAndPrevBar"
    RiskTemplateTicks: int = 16
    RiskToleranceTicks: int = 2
    MaxPullbackPct: float = 49.0
    # Hosted EntryCount counting
    RequireEqualOrHigherLeg: bool = False
    ResetOnKeyLevel: bool = True
    ResetScope: str = "CounterTrendOnly"
    ResetAfterTwoLegs: bool = True
    ResetOnNewExtreme: bool = True
    FailedEntryMode: str = "PriorBarBreak"
    InsideBarAsSignal: bool = True
    # Double Trap
    DtEmaToleranceTicks: int = 2
    DtMaxBarsAfterF2E: int = 10
    DtAllowF2EBarAsSignal: bool = True
    BlockTooLarge: bool = False
    # Weak 2E
    UseWeakEmaGap: bool = True
    EmaGapTicks: int = 4
    UseWeakCongestion: bool = True
    CongestionLookback: int = 6
    CongestionOverlapPct: float = 60.0
    CongestionRangeTicks: int = 12
    WeakIfQualityFails: bool = True
    UseWeakCounterTrend: bool = True
    RequireWeak2E: bool = False
    # Context filters
    RangeLookback: int = 30
    RangeCrossCount: int = 4
    UseRule6Filter: bool = False
    Rule6Lookback: int = 10
    Rule6MinBars: int = 4
    Rule6MaxSpanTicks: int = 26
    Rule6MinEfficiencyPct: float = 5.0
    UseExhaustionFilter: bool = True
    ExhaustionMinTrendBars: int = 20
    # Display
    ShowEntryCountLabels: bool = False
    ShowFirstEntries: bool = True
    ShowWeak2EMarker: bool = True
    ShowInvalidThirdEntry: bool = True
    ShowEntryStopLines: bool = True
    ShowDtRiskTicks: bool = False
    MarkTrigger: bool = True
    LabelFontSize: int = 11
    BarNumberClearance: int = 32
    EnableAlerts: bool = True
    # Colors
    LongColor: str = "LimeGreen"
    ShortColor: str = "Red"
    QualityFailColor: str = "Gray"
    WeakColor: str = "DarkOrange"
    InvalidColor: str = "DimGray"
    StopLineColor: str = "IndianRed"
    TriggerColor: str = "Gold"
    FailedLongColor: str = "Green"
    FailedShortColor: str = "Red"
    # Diagnostics
    DebugLog: bool = False
    ExportCsv: bool = False
    CsvIncludeBars: bool = True


@dataclass
class ChartTraderPaConfig:
    """ChartTrader side (not in NT): which indicators run in the chart and where the CSVs go."""
    ShowEntryCount: bool = True          # EntryCount labels in the chart (its ShowLabels etc. still apply)
    ShowDoubleTrap: bool = True          # DoubleTrap markers in the chart
    TickSize: float = 0.25
    TimeZone: str = "Europe/Berlin"      # NT's time zone: bar times, bar numbers and CSV times
    CsvDir: str = "pa_csv"               # CSV output folder (relative to the ChartTrader folder)
    CsvSuffix: str = "_CT"               # EntryCount_<instrument>_<yyyyMMdd>_CT.csv
    TickTimeResolutionMs: int = 1        # intrabar order: ticks with the bar's closing time stamp count for the next bar


@dataclass
class PaConfig:
    entry_count: EntryCountConfig = field(default_factory=EntryCountConfig)
    double_trap: DoubleTrapConfig = field(default_factory=DoubleTrapConfig)
    chart: ChartTraderPaConfig = field(default_factory=ChartTraderPaConfig)


SECTIONS = {"EntryCount": "entry_count", "DoubleTrap": "double_trap", "ChartTrader": "chart"}
ENUMS = {"OutsideBarPolicy": OUTSIDE_BAR_POLICIES, "StopMode": STOP_MODES, "ResetScope": RESET_SCOPES,
         "FailedEntryMode": FAILED_ENTRY_MODES}


def _apply(target, values: dict, section: str) -> None:
    types = {f.name: f.type for f in fields(target)}
    for key, value in values.items():
        if key not in types:
            raise ValueError(f"[{section}] unknown parameter '{key}'")
        kind = types[key]
        if kind == "bool" and not isinstance(value, bool):
            raise ValueError(f"[{section}] {key} must be true/false")
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"[{section}] {key} must be an integer")
        if kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"[{section}] {key} must be a number")
            value = float(value)
        if key in ENUMS and value not in ENUMS[key]:
            raise ValueError(f"[{section}] {key} must be one of {', '.join(ENUMS[key])}")
        setattr(target, key, value)


def load_config(path: str | Path | None = None) -> PaConfig:
    """Defaults, overridden by the TOML file (missing file = defaults)."""
    cfg = PaConfig()
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        if path:
            raise FileNotFoundError(p)
        return cfg
    with open(p, "rb") as f:
        data = tomllib.load(f)
    for section, values in data.items():
        if section not in SECTIONS:
            raise ValueError(f"unknown section [{section}] in {p}")
        _apply(getattr(cfg, SECTIONS[section]), values, section)
    return cfg
