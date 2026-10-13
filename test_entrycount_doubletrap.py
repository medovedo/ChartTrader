"""pytest: the TEST CASES of the comment blocks in EntryCount.cs (1-9) and DoubleTrap.cs (10-15),
with small constructed bar sequences. Run: python -m pytest test_entrycount_doubletrap.py -q

The events come from the diagnostic lines (DebugLog -> printer), the same lines as in the CSV.
Tick size 0.25. Where a case is about one rule, the other resets are switched off so the bars stay short;
the comment of each test says which parameters differ from the defaults.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from double_trap import DoubleTrap
from entry_count import (RISK_DIRECT, RISK_PULLBACK_LIMIT, RISK_TOO_LARGE, EntryCount, PaBarSnapshot,
                         PaDiagnosticsLog, RiskSettings, SignalBarQuality, SignalBarSettings, StopRiskCalculator)
from entrycount_doubletrap_config import DoubleTrapConfig, EntryCountConfig
from pa_support import DrawText, PaBar, net_custom, net_g

BERLIN = ZoneInfo("Europe/Berlin")
T0 = datetime(2026, 10, 6, 10, 0, tzinfo=BERLIN)
HEADER = PaDiagnosticsLog.Header.split(";")


def bars_of(rows: list[tuple[float, float, float, float]]) -> list[PaBar]:
    return [PaBar(o, h, l, c, T0 + timedelta(minutes=i)) for i, (o, h, l, c) in enumerate(rows)]


def mirror(rows):
    """Price mirror at 100 (long <-> short): high and low swap."""
    return [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in rows]


def parse(lines: list[str]) -> list[dict]:
    return [dict(zip(HEADER, ln.split(";"))) for ln in lines if ln.count(";") >= len(HEADER) - 1]


def run_ec(rows, **params) -> tuple[EntryCount, list[dict]]:
    lines: list[str] = []
    ec = EntryCount(EntryCountConfig(DebugLog=True, **params), printer=lines.append)
    for b in bars_of(rows):
        ec.on_bar_update(b)
    return ec, parse(lines)


def events(ev, kind=None, direction=None):
    return [e for e in ev if (kind is None or e["Event"] == kind) and (direction is None or e["Direction"] == direction)]


def entries(ev, direction):
    """[(bar no of the trigger, count, signal bar no)] of the real entries (not the skipped ones)."""
    return [(int(e["BarNo"]), int(e["Count"]), int(e["SignalBarNo"])) for e in events(ev, "Entry", direction)
            if not e["Reason"].startswith("skipped")]


NO_RESETS = dict(ResetOnKeyLevel=False, ResetOnNewExtreme=False)

# Cases 1-3 (short count). Bar numbers in the comments are the CSV bar numbers (1 = first bar).
SHORT_SEQ = [
    (100.00, 101.00, 99.00, 100.00),     # 1 origin (lowest low)
    (100.00, 102.00, 100.00, 101.75),    # 2 up: leg starts
    (101.75, 103.00, 101.50, 102.75),    # 3 up: signal bar candidate
    (102.75, 102.75, 101.00, 101.25),    # 4 trades below bar 3 low -> 1ES
    (101.50, 103.25, 101.25, 103.00),    # 5 breaks the high of bar 4 -> new up leg
    (103.00, 104.00, 102.75, 103.75),    # 6 bullish bar, breaks no prior low
    (103.50, 103.75, 102.25, 102.50),    # 7 trades below bar 6 low -> 2ES (signal bar 6)
    (102.50, 104.25, 102.25, 104.00),    # 8 trades above the high of bar 7 and of signal bar 6 -> F2ES
]


# --------------------------------------------------------------------------- EntryCount cases 1-9
def test_case_1_first_and_second_short_entry():
    """1. Low -> bullish bars -> break of a low = 1ES -> break of a high -> bullish bar -> break of its low = 2ES."""
    _, ev = run_ec(SHORT_SEQ[:7], **NO_RESETS)
    assert entries(ev, "Short") == [(4, 1, 3), (7, 2, 6)]


def test_case_2_signal_bar_breaks_no_prior_low():
    """2. The 2ES signal bar (bar 6) breaks no prior low, but its own low is traded -> still 2ES."""
    assert SHORT_SEQ[5][2] > SHORT_SEQ[4][2]           # bar 6 low above bar 5 low
    _, ev = run_ec(SHORT_SEQ[:7], **NO_RESETS)
    second = [e for e in events(ev, "Entry", "Short") if e["Count"] == "2"]
    assert len(second) == 1 and second[0]["SignalBarNo"] == "6" and second[0]["Trigger"] == "102,50"


@pytest.mark.parametrize("mode", ["PriorBarBreak", "SignalBarBreak"])
def test_case_3_failed_second_short(mode):
    """3. Price trades above the high of the 2ES signal bar -> F2ES (both FailedEntryModes), then reset R:2L."""
    ec, ev = run_ec(SHORT_SEQ, FailedEntryMode=mode, **NO_RESETS)
    f2 = events(ev, "F2E", "Short")
    assert [int(e["BarNo"]) for e in f2] == [8]
    assert ("prior bar high" in f2[0]["Reason"]) == (mode == "PriorBarBreak")
    resets = events(ev, "Reset", "Short")
    assert resets and resets[-1]["BarNo"] == "8" and resets[-1]["Reason"].startswith("TwoLegs")
    assert ec.shortCount == 0
    f = [o for o in ec.draw.objects.values() if isinstance(o, DrawText) and o.text == "F"]
    assert len(f) == 1 and f[0].bar == 7 and f[0].color == "Red"


def test_case_4_failed_second_long_resets_long_count():
    """4. Downtrend: 2EL/F2EL form a lower high -> the long count resets without a new extreme (R:2L)."""
    ec, ev = run_ec(mirror(SHORT_SEQ), **NO_RESETS)
    assert entries(ev, "Long") == [(4, 1, 3), (7, 2, 6)]
    assert [e["BarNo"] for e in events(ev, "F2E", "Long")] == ["8"]
    resets = events(ev, "Reset", "Long")
    assert [r["Reason"].split(",")[0] for r in resets] == ["TwoLegs"]     # ";" is written as ","
    assert ec.longCount == 0
    assert any(isinstance(o, DrawText) and o.text == "R:2L" for o in ec.draw.objects.values())


def uptrend_with_pullback_to_ema():
    """30 rising bars (EMA slopes up), then a pullback whose bars walk down to the EMA."""
    rows = [(100 + i, 101 + i, 99.75 + i, 100.75 + i) for i in range(30)]
    p = rows[-1][3]
    for k in range(12):                                  # each bar 1 point lower, until the EMA is touched
        o = p - k
        rows.append((o, o + 0.25, o - 1.25, o - 1.0))
    return rows


def test_case_5_key_level_resets_only_the_counter_trend_count():
    """5. Uptrend, pullback to the EMA -> the short count resets (R:KL), the long count keeps running
    (ResetScope CounterTrendOnly)."""
    ec, ev = run_ec(uptrend_with_pullback_to_ema())
    kl_short = [e for e in events(ev, "Reset", "Short") if e["Reason"].startswith("KeyLevel")]
    kl_long = [e for e in events(ev, "Reset", "Long") if e["Reason"].startswith("KeyLevel")]
    assert kl_short, "short count must reset at the EMA"
    assert int(kl_short[0]["Count"]) >= 1 and kl_short[0]["KeyLevel"] == "true" and kl_short[0]["Trend"] == "1"
    assert not kl_long


# Case 6: repeated up legs with lower lows above the origin, resets off -> 1ES, 2ES, 3ES, 4ES
STAIRS = [(100.00, 101.00, 99.00, 100.50)]
for _k in range(4):
    base = 101.0 + _k * 0.25
    STAIRS += [(base, base + 1.0, base - 0.5, base + 0.75),           # up: leg
               (base + 0.75, base + 1.5, base, base + 1.25),          # up: signal bar candidate
               (base + 1.25, base + 1.25, base - 0.25, base)]         # below the candidate low -> entry


def test_case_6_count_continues_without_reset():
    """6. No reset -> the count continues to 3E/4E (ResetAfterTwoLegs, key level and new extreme off)."""
    _, ev = run_ec(STAIRS, ResetAfterTwoLegs=False, **NO_RESETS)
    assert [c for _, c, _ in entries(ev, "Short")] == [1, 2, 3, 4]


def test_case_6_inside_bar_is_signal_bar_never_trigger():
    """6. An inside bar is never a trigger, but (InsideBarAsSignal) can be the signal bar
    (7.10.26: bar 124 inside bar = 2EL signal bar, triggered by bar 125)."""
    rows = [(100.00, 101.00, 99.00, 100.50),
            (100.50, 102.00, 100.00, 101.75),       # 2 up: leg
            (101.75, 103.00, 101.00, 102.75),       # 3 up: candidate
            (102.75, 102.75, 101.50, 102.00),       # 4 inside bar of 3 (low 101.50 >= 101.00): new candidate
            (102.00, 102.50, 101.25, 101.50)]       # 5 below the inside bar low, not below bar 3 low -> 1ES
    _, ev = run_ec(rows, **NO_RESETS)
    assert entries(ev, "Short") == [(5, 1, 4)]
    _, ev = run_ec(rows, InsideBarAsSignal=False, **NO_RESETS)
    assert entries(ev, "Short") == []                # without the option bar 3 stays the signal bar


LOWER_HIGH = [
    (100.00, 101.00, 99.00, 100.00),
    (100.00, 102.00, 100.00, 101.75),       # 2 up
    (101.75, 103.00, 101.50, 102.75),       # 3 up, first leg high 103.00
    (102.50, 102.50, 101.00, 101.25),       # 4 -> 1ES
    (101.50, 102.75, 101.25, 102.50),       # 5 breaks the high of bar 4: up leg, high 102.75 < 103.00 (lower high)
    (102.25, 102.50, 101.00, 101.25),       # 6 below bar 5 low -> 2ES only if lower legs count
]


def test_case_7_lower_high_second_leg():
    """7. The second up leg stays below the first leg high (lower high) -> still 2ES with
    RequireEqualOrHigherLeg false; with true -> no 2ES (logged as skipped)."""
    _, ev = run_ec(LOWER_HIGH, **NO_RESETS)
    assert [c for _, c, _ in entries(ev, "Short")] == [1, 2]
    _, ev = run_ec(LOWER_HIGH, RequireEqualOrHigherLeg=True, **NO_RESETS)
    assert [c for _, c, _ in entries(ev, "Short")] == [1]
    skipped = [e for e in events(ev, "Entry", "Short") if e["Reason"].startswith("skipped")]
    assert len(skipped) == 1 and skipped[0]["Count"] == "2" and skipped[0]["BarNo"] == "6"


def test_case_8_rule5_rejection_exception_and_gray_label():
    """8. 2EL signal bar with body 30 %, close 95 %, upper wick 5 % -> Rule 5 ok via rejection exception;
    body 30 %, close 75 % -> fails (label gray)."""
    s = SignalBarSettings(45, 70, 25, True, 90, 10)
    q = SignalBarQuality.evaluate(True, 106.5, 110.0, 100.0, 109.5, s)
    assert (round(q.BodyPct), round(q.ClosePct), round(q.WickPct)) == (30, 95, 5)
    assert q.Ok and q.IsRejectionBar and q.Reason == "ok (rejection bar)"
    q = SignalBarQuality.evaluate(True, 104.5, 110.0, 100.0, 107.5, s)
    assert (round(q.BodyPct), round(q.ClosePct)) == (30, 75)
    assert not q.Ok and q.Reason == "body 30% < 45%"
    ec = EntryCount(EntryCountConfig())
    ec.CurrentBar = 5
    ec.draw_entry_label(True, 2, PaBarSnapshot(Index=4, High=110, Low=100), False,
                        StopRiskCalculator.compute(True, PaBarSnapshot(High=110, Low=100), 0.25,
                                                   RiskSettings("SignalBarOnly", 16, 2, 49)))
    label = next(iter(ec.draw.objects.values()))
    assert label.text == "2EL" and label.color == "Gray" and label.bar == 4


@pytest.mark.parametrize("risk,cls", [(17, RISK_DIRECT), (22, RISK_PULLBACK_LIMIT), (28, RISK_TOO_LARGE)])
def test_case_9_risk_class(risk, cls):
    """9. Structural risk 22 ticks with RiskTemplateTicks 16 -> PullbackLimit (49 % rule); 28 ticks -> TooLarge;
    17 = max risk 15 + tolerance 2 -> Direct."""
    rng = (risk - 2) * 0.25                     # trigger = high + 1 tick, stop = low - 1 tick
    sig = PaBarSnapshot(High=100 + rng, Low=100.0, PrevHigh=100 + rng, PrevLow=100.0, HasPrev=True)
    r = StopRiskCalculator.compute(True, sig, 0.25, RiskSettings("SignalAndPrevBar", 16, 2, 49))
    assert r.RiskTicks == risk and r.RiskClass == cls
    if cls == RISK_PULLBACK_LIMIT:
        assert r.LimitPrice == r.Stop + 15 * 0.25


# --------------------------------------------------------------------------- formatting (CSV parity)
def test_net_formatting_like_dotnet_framework():
    assert net_custom(7819.78785, 2, 4) == "7819,7879"        # 15 digits, then half away from zero
    assert net_custom(7826.5, 2, 4) == "7826,50"
    assert net_custom(-0.04, 0, 1) == "0"                     # no "-0" on .NET Framework
    assert net_custom(-12.75, 0, 1) == "-12,8"
    assert net_custom(62.5, 0, 0) == "63"
    assert net_g(7826.5) == "7826.5" and net_g(7840.0) == "7840"


# --------------------------------------------------------------------------- DoubleTrap cases 10-15
def run_dt(rows, **params) -> tuple[DoubleTrap, list[dict]]:
    lines: list[str] = []
    dt = DoubleTrap(DoubleTrapConfig(DebugLog=True, **params), printer=lines.append)
    for b in bars_of(rows):
        dt.on_bar_update(b)
    return dt, parse(lines)


def _ema(rows, period=21):
    k, e = 2 / (period + 1), None
    for r in rows:
        e = r[3] if e is None else r[3] * k + e * (1 - k)
    return e


def long_dt_base():
    """Uptrend (EMA slopes up), pullback in two legs: 1EL (bar 32 signal), 2EL (bar 35 signal, trigger 36),
    F2EL on bar 37, then lower lows down to the EMA. The next bar decides the Double Trap."""
    rows = [(100 + i, 101 + i, 99.75 + i, 100.75 + i) for i in range(30)]
    rows += [(129.75, 129.75, 128.50, 128.75),   # 31 down: long leg
             (128.75, 129.00, 127.75, 128.00),   # 32 down: 1EL signal bar
             (128.00, 129.50, 128.00, 129.25),   # 33 above bar 32 high -> 1EL
             (129.25, 129.25, 127.00, 127.25),   # 34 down
             (127.25, 127.50, 126.00, 126.25),   # 35 down: 2EL signal bar
             (126.25, 128.00, 126.25, 127.75),   # 36 above bar 35 high -> 2EL (with the trend)
             (127.75, 127.75, 125.50, 125.75)]   # 37 below bar 36 low -> F2EL
    while rows[-1][2] - 1.0 > _ema(rows) + 0.5:  # lower lows until the EMA is near
        o, lo = rows[-1][3], rows[-1][2]
        rows.append((o, o + 0.25, lo - 1.0, lo - 0.75))
    return rows


LONG_SIGNAL_BAR = (124.25, 126.00, 124.00, 125.75)     # new low at/through the EMA, body 75 %, close 88 %
LONG_TRIGGER_BAR = (125.75, 126.50, 125.50, 126.25)    # trades the entry (signal high + 1 tick)


def test_case_11_long_double_trap():
    """11. Uptrend, 2EL not yet at the EMA, counter-trend structure intact -> new low at the EMA, bullish bar
    closes above the EMA -> long DT, even though EntryCount reset in between (R:2L after the F2EL)."""
    rows = long_dt_base() + [LONG_SIGNAL_BAR, LONG_TRIGGER_BAR]
    sig_no, trg_no = len(rows) - 1, len(rows)
    dt, ev = run_dt(rows)
    assert [e["BarNo"] for e in events(ev, "Weak2E", "Long")] == ["36"]      # far from the EMA: weak 2E
    assert [e["BarNo"] for e in events(ev, "DtCandidate", "Long")][0] == "37"
    signals = events(ev, "DtSignal", "Long")
    assert [int(e["BarNo"]) for e in signals] == [sig_no, trg_no]
    assert signals[0]["Trigger"] == "126,25" and signals[0]["Stop"] == "123,75" and signals[0]["QualityOk"] == "true"
    assert signals[1]["Reason"] == f"entry triggered (signal bar {sig_no})"
    _, ec_ev = run_ec(rows)                          # the hosted EntryCount logs nothing; same rules standalone
    assert [e["BarNo"] for e in events(ec_ev, "Reset", "Long") if e["Reason"].startswith("TwoLegs")] == ["37"]
    texts = {o.text for o in dt.draw.objects.values() if isinstance(o, DrawText)}
    assert {"▲ DT", "DT?"} <= texts
    assert {"DT_E_" + str(sig_no - 1), "DT_SL_" + str(sig_no - 1), "DT_TRG_" + str(trg_no - 1)} <= set(dt.draw.objects)


def test_case_10_short_double_trap():
    """10. Downtrend, 2ES far above the EMA -> F2ES -> bearish bar closes below the EMA -> short DT."""
    rows = mirror(long_dt_base() + [LONG_SIGNAL_BAR, LONG_TRIGGER_BAR])
    _, ev = run_dt(rows)
    assert [int(e["BarNo"]) for e in events(ev, "DtSignal", "Short")] == [len(rows) - 1, len(rows)]
    assert not events(ev, "DtSignal", "Long")


def test_case_12_close_below_ema_is_third_entry_invalid():
    """12. Like 11, but the close stays below the EMA -> no signal (ThirdEntryInvalid, 'DT x')."""
    rows = long_dt_base() + [(123.50, 123.75, 123.00, 123.50)]
    dt, ev = run_dt(rows)
    inv = events(ev, "ThirdEntryInvalid", "Long")
    assert [int(e["BarNo"]) for e in inv] == [len(rows)] and inv[0]["Reason"] == "close not above EMA"
    assert not events(ev, "DtSignal")
    assert any(isinstance(o, DrawText) and o.text == "DT x" for o in dt.draw.objects.values())


def test_case_13_signal_bar_fails_rule5():
    """13. Like 11, but the signal bar fails Rule 5 (close 60 %) -> no signal, DtRejected with the reason."""
    rows = long_dt_base() + [(124.25, 126.50, 124.00, 125.50)]
    _, ev = run_dt(rows)
    rej = [e for e in events(ev, "DtRejected", "Long") if int(e["BarNo"]) == len(rows)]
    assert [e["Reason"] for e in rej] == ["quality: close 60% < 70%"]
    assert not events(ev, "DtSignal")


def _candidate_reason(dt: DoubleTrap, ev_lines: list[str], trend: int) -> str:
    """Evaluates the current bar as long DT candidate (stage AwaitSignal) and returns the logged reason."""
    s = dt.longSide
    s.Stage, s.F2Bar = 2, dt.CurrentBar
    s.SeqFirstExt, s.SecondSigLow, s.CorrExt, s.CorrExtBar = float("nan"), 1e9, 0.0, dt.CurrentBar
    n = len(ev_lines)
    dt.evaluate_candidate(s, dt.ema(0), trend, False)
    return parse(ev_lines[n:])[-1]["Reason"]


def test_case_14_range_filter():
    """14. Flat EMA in a range (closes cross the EMA again and again) -> no signal (range filter)."""
    rows = [(100.00, 101.00, 99.00, 100.75 if i % 2 else 99.25) for i in range(40)]
    lines: list[str] = []
    dt = DoubleTrap(DoubleTrapConfig(DebugLog=True), printer=lines.append)
    for b in bars_of(rows):
        dt.on_bar_update(b)
    assert dt.ec.trendState == 0 and dt.count_ema_crosses() >= 4
    assert _candidate_reason(dt, lines, 0).startswith("range filter: ")


def test_case_15_exhaustion_filter():
    """15. Trend broken (close below the EMA after 20+ closes above) and then a new high -> no long DT (exhaustion)."""
    rows = [(100 + i, 101 + i, 99.75 + i, 100.75 + i) for i in range(25)]
    rows += [(124.75, 125.00, 114.00, 114.50),          # first close below the EMA: trend break
             (114.50, 126.00, 114.25, 125.50)]          # new high above the run high: exhausted
    lines: list[str] = []
    dt = DoubleTrap(DoubleTrapConfig(DebugLog=True), printer=lines.append)
    for b in bars_of(rows):
        dt.on_bar_update(b)
    assert dt.longSide.ExState == 2 and dt.ec.trendState == 1
    assert _candidate_reason(dt, lines, 1) == "exhaustion: new high after trend break"


# --------------------------------------------------------------------------- tuning EC-11 (CHANGELOG Nr. 1)
def ec11_rows():
    """Uptrend, then 1ES (bar 15), 2ES (18), F2ES (19); an up leg and a would-be 1ES (21) while the price stays
    above the EMA; then down bars until the first close below the EMA (bar 25)."""
    rise = [(100 + i, 101 + i, 99.75 + i, 100.75 + i) for i in range(12)]
    top = rise[-1][3]
    seq = [(o + top - 100, h + top - 100, l + top - 100, c + top - 100) for o, h, l, c in SHORT_SEQ[1:]]
    c0 = seq[-1][3]
    rows = rise + seq + [(c0, c0 + 1.0, c0 - 0.25, c0 + 0.75), (c0 + 0.75, c0 + 1.0, c0 - 0.5, c0 - 0.25)]
    p = rows[-1][3]
    for _ in range(4):
        rows.append((p, p + 0.25, p - 1.5, p - 1.25))
        p -= 1.25
    return rows


def test_ec11_pause_short_count_after_f2es_in_uptrend():
    """EC-11: F2ES in an uptrend pauses the short count until a close below the EMA (option on);
    without the option bar 21 is a 1ES."""
    rows = ec11_rows()
    _, ev = run_ec(rows, ResetOnKeyLevel=False)
    assert (21, 1, 20) in entries(ev, "Short") and not events(ev, "CountPaused")
    ec, ev = run_ec(rows, ResetOnKeyLevel=False, PauseCountAfterCounterTrendF2E=True)
    paused, resumed = events(ev, "CountPaused", "Short"), events(ev, "CountResumed", "Short")
    assert [e["BarNo"] for e in paused] == ["19"] and paused[0]["Trend"] == "1"
    assert [e["BarNo"] for e in resumed] == ["25"] and "below EMA" in resumed[0]["Reason"]
    assert [b for b, _, _ in entries(ev, "Short")] == [15, 18]          # nothing between pause and resume
    mid, _ = run_ec(rows[:22], ResetOnKeyLevel=False, PauseCountAfterCounterTrendF2E=True)
    assert mid.shortState.Paused and not mid.longState.Paused                 # after bar 22: still paused
    assert not ec.shortState.Paused


def test_ec11_mirror_long_count_after_f2el_in_downtrend():
    """EC-11 mirrored: F2EL in a downtrend pauses the long count until a close above the EMA."""
    _, ev = run_ec(mirror(ec11_rows()), ResetOnKeyLevel=False, PauseCountAfterCounterTrendF2E=True)
    assert [e["BarNo"] for e in events(ev, "CountPaused", "Long")] == ["19"]
    assert [e["BarNo"] for e in events(ev, "CountResumed", "Long")] == ["25"]
    assert [b for b, _, _ in entries(ev, "Long")] == [15, 18]


# --------------------------------------------------------------------------- tuning EC-13 (CHANGELOG Nr. 7)
def test_ec13_running_count_goes_to_f2es_above_the_ema_then_pauses():
    """EC-13: the short count started at the EMA (first bar) runs above the EMA to its F2ES (bar 19, close above
    the EMA), then pauses until the first close below the EMA (bar 25) - without the trend condition of EC-11."""
    ec, ev = run_ec(ec11_rows(), ResetOnKeyLevel=False, CountOnlyFromEmaSide=True)
    paused, resumed = events(ev, "CountPaused", "Short"), events(ev, "CountResumed", "Short")
    assert [e["BarNo"] for e in paused] == ["19"] and paused[0]["Reason"].startswith("F2ES with close above the EMA")
    assert [e["BarNo"] for e in resumed] == ["25"]
    assert [b for b, _, _ in entries(ev, "Short")] == [15, 18]
    assert not ec.shortState.Paused
    _, ev = run_ec(mirror(ec11_rows()), ResetOnKeyLevel=False, CountOnlyFromEmaSide=True)
    assert [e["BarNo"] for e in events(ev, "CountPaused", "Long")] == ["19"]
    assert [b for b, _, _ in entries(ev, "Long")] == [15, 18]


def test_ec13_no_new_count_beyond_the_ema():
    """EC-13: a new high (reset d) on a bar closing below the EMA would start the long count from 0 below the EMA:
    paused until the next close above the EMA, where the count starts again (origin = that bar)."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * 5 + [(100.0, 101.0, 98.0, 98.5),   # new high, close below the EMA
                                                 (98.5, 100.5, 98.25, 100.25)]  # close above the EMA
    _, ev = run_ec(rows, ResetOnKeyLevel=False)
    assert not events(ev, "CountPaused")
    ec, ev = run_ec(rows, ResetOnKeyLevel=False, CountOnlyFromEmaSide=True)
    paused, resumed = events(ev, "CountPaused", "Long"), events(ev, "CountResumed", "Long")
    assert [e["BarNo"] for e in paused] == ["6"]
    assert paused[0]["Reason"] == "count would start with close below the EMA: long count paused until a close above the EMA"
    assert [e["BarNo"] for e in resumed] == ["7"] and not ec.longState.Paused
    assert ec.longState.OriginPrice == 100.5 and ec.longState.Count == 0
    assert not events(ev, "CountPaused", "Short")


# --------------------------------------------------------------------------- tuning DT-a / DT-b (CHANGELOG Nr. 3, 4)
def test_dta_signal_bar_must_be_the_correction_low():
    """DT-a: a bar after the correction low (no counter move since) is a signal only with the option off."""
    x = (124.50, 126.25, 123.75, 123.75)          # new correction low, closes below the EMA (DT x)
    z = (124.50, 125.50, 124.50, 125.00)          # inside bar of x
    y = (124.00, 125.50, 124.00, 125.25)          # higher low than x, no break of z's high (no counter move): bullish
    rows = long_dt_base() + [x, z, y]
    _, ev = run_dt(rows)
    assert [int(e["BarNo"]) for e in events(ev, "DtSignal", "Long")] == [len(rows)]
    _, ev = run_dt(rows, DtSignalBarIsCorrectionExtreme=True)
    assert not events(ev, "DtSignal")
    rej = [e for e in events(ev, "DtRejected", "Long") if int(e["BarNo"]) == len(rows)]
    assert [e["Reason"] for e in rej] == [f"bar is not the correction low (123.75 @bar {len(rows) - 2})"]
    _, ev = run_dt(long_dt_base() + [LONG_SIGNAL_BAR], DtSignalBarIsCorrectionExtreme=True)
    assert events(ev, "DtSignal", "Long")                 # the signal bar is the low itself: still a signal


def test_dtb_correction_must_reach_the_entry_stops():
    """DT-b: below the 2E signal bar low is not enough, the correction low must reach the 2E stop."""
    rows = long_dt_base() + [LONG_SIGNAL_BAR]
    for on in (False, True):
        lines: list[str] = []
        dt = DoubleTrap(DoubleTrapConfig(DebugLog=True, DtRequireEntryStopsHit=on), printer=lines.append)
        for b in bars_of(rows):
            dt.on_bar_update(b)
        s = dt.longSide
        assert s.SeqFirstStop == 127.50 and s.SecondStop == 125.75      # min(signal low, prev low) - 1 tick
        s.Stage, s.SecondStop, s.CorrExt, s.CorrExtBar = 2, 125.00, 125.50, dt.CurrentBar   # below 126, above 125
        s.SeqFirstExt, s.SeqFirstStop = float("nan"), float("nan")
        n = len(lines)
        dt.evaluate_candidate(s, dt.ema(0), 1, False)
        reasons = [e["Reason"] for e in parse(lines[n:]) if e["Event"] == "DtRejected"]
        assert any(r.startswith("no new low at/beyond the 2EL stop (125)") for r in reasons) == on
