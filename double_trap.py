"""Port of the NinjaTrader 8 indicator DoubleTrap.cs (Documents\\NinjaTrader 8\\bin\\Custom\\Indicators).

Port, not an improvement (see entry_count.py for the conventions). Line numbers refer to DoubleTrap.cs as of
2026-10-09 (1375 lines). DoubleTrap hosts its own EntryCount (ShowLabels = false, no CSV) and reads its series
after `ec.Update()`; here the host feeds the hosted instance first in `on_bar_update` / `on_tick`.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Callable

from entry_count import (NAN, RISK_NONE, RISK_PULLBACK_LIMIT, RISK_TOO_LARGE, DayBarNumbering,
                         EntryCount, PaBarContext, PaBarSnapshot, PaDiagnosticsLog, PaLabelLayout, RiskResult,
                         RiskSettings, SignalBarQuality, SignalBarResult, SignalBarSettings, StopRiskCalculator)
from entrycount_doubletrap_config import DoubleTrapConfig, EntryCountConfig
from pa_support import DrawDiamond, DrawLine, DrawText, Drawings, PaBar, net_custom, net_g, trading_day_of

# State constants, lines 42-53
StageIdle = 0
StageAwaitF2E = 1
StageAwaitSignal = 2
StagePending = 3
StageDone = 4

ExNormal = 0
ExBroken = 1
ExExhausted = 2

MaxScanBars = 300
HostedLabelMaxCount = 3

# Parameters passed to the hosted EntryCount (factory call, lines 241-246)
HOSTED_PARAMS = ("EmaPeriod", "KeyLevelToleranceTicks", "SwingStrength", "UseSwingSR", "SrLookbackSwings",
                 "SrToleranceTicks", "UseTrendlines", "OutsideBarPolicy", "EmaSlopeLookback", "MinEmaSlopeTicks",
                 "MinBodyPct", "MinClosePct", "MaxBreakoutWickPct", "UseRejectionException", "RejectionMinClosePct",
                 "RejectionMaxWickPct", "StopMode", "RiskTemplateTicks", "RiskToleranceTicks", "MaxPullbackPct",
                 "RequireEqualOrHigherLeg", "ResetOnKeyLevel", "ResetScope", "ResetAfterTwoLegs", "ResetOnNewExtreme",
                 "FailedEntryMode", "InsideBarAsSignal",
                 # tuning CHANGELOG Nr. 9: the hosted EntryCount counts like EntryCount (EC-11 .. EC-14)
                 "PauseCountAfterCounterTrendF2E", "F2EWatchUntil2EOrigin", "CountOnlyFromEmaSide", "ResetOnEqualExtreme")


def _f0(x: float) -> str:
    """string.Format("{0:0}", x) with invariant culture."""
    return net_custom(x, 0, 0, ".")


class DtSide:
    """port of DoubleTrap.DtSide, lines 55-93 (sequence and context state of one direction)."""

    def __init__(self, is_long: bool):
        self.IsLong = is_long
        # Last 1E reported by EntryCount (condition 3 and weak criterion d)
        self.FirstSignalExt = NAN
        self.FirstTriggerBar = -1
        self.FirstStop = NAN               # tuning DT-b: structural stop of that 1E
        self.SeqFirstStop = NAN            # tuning DT-b: 1E stop of the running sequence
        self.SecondStop = 0.0              # tuning DT-b: structural stop of the 2E
        self.LastSeenSecondBar = -1
        # Sequence 2E -> F2E -> signal bar
        self.Stage = 0
        self.SecondTriggerBar = 0
        self.SecondSignalBar = 0
        self.SecondSigHigh = 0.0
        self.SecondSigLow = 0.0
        self.SeqFirstExt = 0.0
        self.PullStartExt = 0.0
        self.PullStartBar = 0
        self.Weak = False
        self.F2Bar = 0
        self.CorrExt = 0.0
        self.CorrExtBar = 0
        self.CounterBreakSinceCorr = False
        # Pending signal
        self.SigBar = 0
        self.SigHigh = 0.0
        self.SigLow = 0.0
        self.Entry = 0.0
        self.Stop = 0.0
        # Exhaustion
        self.ExState = 0
        self.RunLen = 0
        self.RunExt = 0.0
        self.BrokenExt = 0.0
        self.RunSinceEx = 0


class DoubleTrap:
    """port of class DoubleTrap, lines 38-1375."""
    Name = "DoubleTrap"

    def __init__(self, cfg: DoubleTrapConfig | None = None, tick_size: float = 0.25,
                 instrument_name: str | None = None, csv_dir: str | Path = ".", csv_suffix: str = "",
                 draw: Drawings | None = None, printer: Callable[[str], None] | None = None):
        # port of DoubleTrap.OnStateChange, State.DataLoaded, lines 236-287
        self.cfg = cfg = replace(cfg) if cfg is not None else DoubleTrapConfig()
        self.TickSize = tick_size
        self.draw = draw if draw is not None else Drawings()
        self.printer = printer or print

        self.bars: list[PaBar] = []
        self.ema_values: list[float] = []
        self._ema_c1 = 2.0 / (1 + cfg.EmaPeriod)
        self._ema_c2 = 1 - 2.0 / (1 + cfg.EmaPeriod)
        self.CurrentBar = -1

        # Shared parameters are passed through; the hosted instance never draws.
        hosted = EntryCountConfig(**{k: getattr(cfg, k) for k in HOSTED_PARAMS})
        hosted.ShowLabels = False
        self.ec = EntryCount(hosted, tick_size, instrument_name, printer=self.printer)

        self.barNumbering = DayBarNumbering()
        self.longSide = DtSide(True)
        self.shortSide = DtSide(False)

        self.signalSettings = SignalBarSettings(cfg.MinBodyPct, cfg.MinClosePct, cfg.MaxBreakoutWickPct,
                                                cfg.UseRejectionException, cfg.RejectionMinClosePct,
                                                cfg.RejectionMaxWickPct)
        self.riskSettings = RiskSettings(cfg.StopMode, cfg.RiskTemplateTicks, cfg.RiskToleranceTicks,
                                         cfg.MaxPullbackPct)

        # Series<T>[0] of the current bar
        self.signal = 0
        self.entryPrice = self.stopPrice = self.limitPrice = 0.0
        self.riskTicks = self.riskClass = 0
        self.twoLegVisual = False
        self.weak2E = False

        self.halfTick = tick_size * 0.5
        self.rowHeight = PaLabelLayout.row_height(cfg.LabelFontSize)
        self.labelFontSize = cfg.LabelFontSize
        self.smallFontSize = max(7, cfg.LabelFontSize - 2)

        self.diag: PaDiagnosticsLog | None = None
        if cfg.DebugLog or cfg.ExportCsv:
            self.diag = PaDiagnosticsLog("DoubleTrap", instrument_name, csv_dir, csv_suffix)
        self.tradingDay: date | None = None

    def terminate(self) -> None:
        # port of DoubleTrap.OnStateChange, State.Terminated, lines 293-300
        if self.diag is not None:
            self.diag.close()
        self.ec.terminate()

    # --- NinjaScript series access (barsAgo / GetValueAt) -------------------------------------
    def _bar(self, ago: int) -> PaBar:
        return self.bars[self.CurrentBar - ago]

    def ema(self, ago: int) -> float:
        return self.ema_values[self.CurrentBar - ago]

    def _update_ema(self, close: float) -> None:
        if not self.ema_values:
            self.ema_values.append(close)
        else:
            self.ema_values.append(close * self._ema_c1 + self._ema_c2 * self.ema_values[-1])

    # --- OnBarUpdate ----------------------------------------------------------------------
    def on_tick(self, price: float) -> None:
        """OnBarUpdate with BarsInProgress 1: ec.Update() processes the hosted tick series, then return."""
        self.ec.on_tick(price)

    def on_bar_update(self, bar: PaBar) -> None:
        # port of DoubleTrap.OnBarUpdate, lines 303-372
        self.ec.on_bar_update(bar)          # ec.Update(): the hosted indicator is processed first
        self.bars.append(bar)
        self.CurrentBar = cb = len(self.bars) - 1
        self._update_ema(bar.close)
        cfg, ec = self.cfg, self.ec

        self.tradingDay = trading_day_of(bar.time)

        self.signal = 0
        self.entryPrice = 0.0
        self.stopPrice = 0.0
        self.limitPrice = 0.0
        self.riskTicks = 0
        self.riskClass = 0
        self.twoLegVisual = False
        self.weak2E = False

        bar_no = self.barNumbering.update(cb, bar.time, self._bar(1).time if cb > 0 else bar.time)
        if cb < 2:
            return

        ema0 = self.ema(0)
        trend = ec.trendState
        b1 = self._bar(1)
        inside = bar.high <= b1.high + self.halfTick and bar.low >= b1.low - self.halfTick

        if self.diag is not None:
            self.diag.Context = PaBarContext(
                BarNo=bar_no, BarEnd=bar.time, BarStart=b1.time,
                Open=bar.open, High=bar.high, Low=bar.low, Close=bar.close, Ema=ema0,
                BarType="Inside" if inside else "", Trend=trend, KeyLevel=ec.emaTouch[ec.CurrentBar])

        self.update_exhaustion(self.longSide, ema0, trend)
        self.update_exhaustion(self.shortSide, ema0, trend)

        if cfg.ShowEntryCountLabels:
            self.draw_hosted_labels()

        self.process_side(self.longSide, ema0, trend, inside)
        self.process_side(self.shortSide, ema0, trend, inside)

        if self.diag is not None:
            # Per-bar line: counts from EntryCount, state columns show the DT sequence stage.
            ctx = self.diag.Context
            ctx.LongCount = ec.longCount
            ctx.ShortCount = ec.shortCount
            ctx.LongStateText = self.stage_text(self.longSide)
            ctx.ShortStateText = self.stage_text(self.shortSide)
            if cfg.ExportCsv and cfg.CsvIncludeBars:
                self.diag.write_csv(self.tradingDay, self.diag.format_bar())

    @staticmethod
    def stage_text(s: DtSide) -> str:
        # port of DoubleTrap.StageText, lines 374-384
        return {StageAwaitF2E: "DT: 2E seen, waiting for F2E",
                StageAwaitSignal: "DT: F2E seen, waiting for signal bar",
                StagePending: "DT: signal pending",
                StageDone: "DT: triggered"}.get(s.Stage, "DT: idle")

    # --- Sequence (lines 386-770) ---------------------------------------------------------
    def process_side(self, s: DtSide, ema0: float, trend: int, inside: bool) -> None:
        # port of DoubleTrap.ProcessSide, lines 388-478
        is_long, cfg, ec, cb, half = s.IsLong, self.cfg, self.ec, self.CurrentBar, self.halfTick
        b0, b1 = self._bar(0), self._bar(1)

        entry_event = ec.longEntryEvent if is_long else ec.shortEntryEvent
        if entry_event == 1:
            s.FirstSignalExt = ec.longSignalLow if is_long else ec.shortSignalHigh
            s.FirstTriggerBar = cb
            s.FirstStop = ec.longStop if is_long else ec.shortStop          # tuning DT-b

        # Pending signal from an earlier bar: entry triggered, or cancelled by a new correction extreme.
        if s.Stage == StagePending and cb > s.SigBar:
            if b0.high >= s.Entry - half if is_long else b0.low <= s.Entry + half:
                s.Stage = StageDone
                if cfg.MarkTrigger:
                    self.draw.put(DrawDiamond("DT_TRG_" + str(cb), cb, s.Entry, cfg.TriggerColor))
                self.extend_lines(s)
                self.log_dt("DtSignal", is_long, 3, NAN, None, s.Entry, s.Stop, 0, RISK_NONE,
                            f"entry triggered (signal bar {self.barNumbering.get(s.SigBar)})")
            elif b0.low < s.SigLow - half if is_long else b0.high > s.SigHigh + half:
                s.Stage = StageAwaitSignal
                self.log_dt("DtRejected", is_long, 3, ema0, None, NAN, NAN, 0, RISK_NONE,
                            "pending signal cancelled: new correction extreme before trigger")
            else:
                self.extend_lines(s)

        # A new 2E (raw event log, independent of resets) starts a new sequence.
        last_second = ec.lastSecondLongBar if is_long else ec.lastSecondShortBar
        if last_second == cb and last_second != s.LastSeenSecondBar:
            s.LastSeenSecondBar = last_second
            self.start_sequence(s, trend)

            # Same-bar F2E: the outside bar that triggered the 2E already failed it (entry and exit at once).
            if s.Stage == StageAwaitF2E and (ec.failedSecondLong if is_long else ec.failedSecondShort):
                s.Stage = StageAwaitSignal
                s.F2Bar = cb
                self.log_dt("DtCandidate", is_long, 3, ema0, None, NAN, NAN, 0, RISK_NONE,
                            "sequence 2EL -> F2EL complete on the 2E bar, waiting for signal bar" if is_long
                            else "sequence 2ES -> F2ES complete on the 2E bar, waiting for signal bar")
            return

        if s.Stage == StageIdle or s.Stage == StageDone:
            return

        # Expiry: new trend extreme beyond the start of the pullback, or too many bars after the F2E.
        if b0.high > s.PullStartExt + half if is_long else b0.low < s.PullStartExt - half:
            self.expire_sequence(s, ema0, "sequence expired: new high above pullback start" if is_long
                                 else "sequence expired: new low below pullback start")
            return
        if s.Stage >= StageAwaitSignal and cb - s.F2Bar > cfg.DtMaxBarsAfterF2E:
            self.expire_sequence(s, ema0, f"sequence expired: {cb - s.F2Bar} bars after F2E > {cfg.DtMaxBarsAfterF2E}")
            return

        # Correction extreme since the 2E trigger and whether a counter move started after it.
        if b0.low < s.CorrExt - half if is_long else b0.high > s.CorrExt + half:
            s.CorrExt = b0.low if is_long else b0.high
            s.CorrExtBar = cb
            s.CounterBreakSinceCorr = False
        elif not inside and (b0.high > b1.high + half if is_long else b0.low < b1.low - half):
            s.CounterBreakSinceCorr = True

        if s.Stage == StageAwaitF2E and (ec.failedSecondLong if is_long else ec.failedSecondShort):
            s.Stage = StageAwaitSignal
            s.F2Bar = cb
            self.log_dt("DtCandidate", is_long, 3, ema0, None, NAN, NAN, 0, RISK_NONE,
                        "sequence 2EL -> F2EL complete, waiting for signal bar" if is_long
                        else "sequence 2ES -> F2ES complete, waiting for signal bar")

        if s.Stage == StageAwaitSignal and (cfg.DtAllowF2EBarAsSignal or cb > s.F2Bar):
            self.evaluate_candidate(s, ema0, trend, inside)

    def start_sequence(self, s: DtSide, trend: int) -> None:
        # port of DoubleTrap.StartSequence, lines 480-530
        is_long, ec, cb, cfg = s.IsLong, self.ec, self.CurrentBar, self.cfg
        sig_bar = ec.longSignalBar if is_long else ec.shortSignalBar
        sig_ago = cb - sig_bar

        s.Stage = StageIdle
        if sig_bar < 1 or sig_ago > 250:
            return

        b0 = self._bar(0)
        s.SecondTriggerBar = cb
        s.SecondSignalBar = sig_bar
        s.SecondSigHigh = ec.longSignalHigh if is_long else ec.shortSignalHigh
        s.SecondSigLow = ec.longSignalLow if is_long else ec.shortSignalLow
        s.PullStartExt = ec.longEntryOriginPrice if is_long else ec.shortEntryOriginPrice
        s.PullStartBar = ec.longEntryOriginBar if is_long else ec.shortEntryOriginBar

        # The 1E of the same count must lie between the pullback start and this 2E.
        s.SeqFirstExt = s.FirstSignalExt if s.PullStartBar < s.FirstTriggerBar < cb else NAN
        s.SeqFirstStop = s.FirstStop if s.PullStartBar < s.FirstTriggerBar < cb else NAN      # tuning DT-b
        s.SecondStop = ec.longStop if is_long else ec.shortStop                              # tuning DT-b

        s.CorrExt = b0.low if is_long else b0.high
        s.CorrExtBar = cb
        s.CounterBreakSinceCorr = False

        sig = self._bar(sig_ago)
        sig_open = sig.open
        sig_close = sig.close
        ema_at_sig = self.ema(sig_ago)

        # ASSUMPTION: "with the trend" is checked at the 2E bar; at the signal bar the EMA must not slope against it.
        if trend != (1 if is_long else -1):
            self.log_dt("DtRejected", is_long, 2, ema_at_sig, None, NAN, NAN, 0, RISK_NONE,
                        f"2E{'L' if is_long else 'S'} not with trend (trend state {trend})",
                        s.SecondSigHigh, s.SecondSigLow, sig_open, sig_close, sig_bar)
            return

        s.Stage = StageAwaitF2E
        weak_reasons = self.evaluate_weak_2e(s, sig_ago, ema_at_sig)
        s.Weak = weak_reasons is not None

        if s.Weak:
            self.weak2E = True
            if cfg.ShowWeak2EMarker:
                self.draw.put(DrawText("DT_W_" + str(cb), "DT?", cb - sig_ago,
                                       s.SecondSigLow if is_long else s.SecondSigHigh, self.label_offset(is_long, 2),
                                       cfg.WeakColor, self.smallFontSize, False))
            self.log_dt("Weak2E", is_long, 2, ema_at_sig, None, NAN, NAN, 0, RISK_NONE,
                        weak_reasons, s.SecondSigHigh, s.SecondSigLow, sig_open, sig_close, sig_bar)

    def evaluate_weak_2e(self, s: DtSide, sig_ago: int, ema_at_sig: float) -> str | None:
        # port of DoubleTrap.EvaluateWeak2E, lines 533-605
        is_long, cfg, cb = s.IsLong, self.cfg, self.CurrentBar
        reasons = None

        # a) gap between the 2E signal bar and the EMA
        if cfg.UseWeakEmaGap:
            gap = (s.SecondSigLow - ema_at_sig if is_long else ema_at_sig - s.SecondSigHigh) / self.TickSize
            if gap > cfg.EmaGapTicks:
                reasons = self.append_reason(reasons, f"ema gap {_f0(gap)}T > {cfg.EmaGapTicks}T")

        # b) congestion over the last bars
        if cfg.UseWeakCongestion:
            n = min(cfg.CongestionLookback, cb)
            if n >= 2:
                b0 = self._bar(0)
                hi, lo, overlap_sum = b0.high, b0.low, 0.0
                for k in range(n - 1):
                    bk, bk1 = self._bar(k), self._bar(k + 1)
                    hi = max(hi, bk1.high)
                    lo = min(lo, bk1.low)
                    ov = min(bk.high, bk1.high) - max(bk.low, bk1.low)
                    min_range = min(bk.high - bk.low, bk1.high - bk1.low)
                    overlap_sum += 0 if ov <= 0 else (100 if min_range <= 0 else min(100, ov / min_range * 100))
                avg_overlap = overlap_sum / (n - 1)
                span_ticks = (hi - lo) / self.TickSize
                if avg_overlap >= cfg.CongestionOverlapPct:
                    reasons = self.append_reason(
                        reasons, f"congestion overlap {_f0(avg_overlap)}% >= {_f0(cfg.CongestionOverlapPct)}%")
                elif span_ticks < cfg.CongestionRangeTicks:
                    reasons = self.append_reason(
                        reasons, f"congestion span {_f0(span_ticks)}T < {cfg.CongestionRangeTicks}T")

        # c) wrong signal bar direction or Rule 5 failed
        if cfg.WeakIfQualityFails and not (self.ec.longSignalQualityOk if is_long else self.ec.shortSignalQualityOk):
            reasons = self.append_reason(reasons, "signal bar fails Rule 5")

        # d) short-term counter-trend structure intact. ASSUMPTION: swing = extreme between 1E trigger and 2E signal bar.
        if cfg.UseWeakCounterTrend and s.SeqFirstExt == s.SeqFirstExt:
            frm = max(s.FirstTriggerBar, cb - MaxScanBars)
            to = s.SecondSignalBar
            if frm <= to:
                swing = -1.7976931348623157e308 if is_long else 1.7976931348623157e308
                swing_bar = frm
                for i in range(frm, to + 1):
                    v = self.bars[i].high if is_long else self.bars[i].low
                    if v > swing if is_long else v < swing:
                        swing = v
                        swing_bar = i
                intact = True
                i = swing_bar
                while i <= cb and intact:
                    c = self.bars[i].close
                    if c > swing if is_long else c < swing:
                        intact = False
                    i += 1
                if intact:
                    reasons = self.append_reason(reasons, f"counter-trend structure intact (swing {net_g(swing)})")
        return reasons

    @staticmethod
    def append_reason(current: str | None, add: str) -> str:
        # port of DoubleTrap.AppendReason, lines 607-610
        return add if current is None else current + ", " + add

    def _stop_hit(self, is_long: bool, extreme: float, stop: float) -> bool:
        """Tuning DT-b: a stop is hit when price trades at it (long stop below: low <= stop)."""
        return extreme <= stop + self.halfTick * 0.5 if is_long else extreme >= stop - self.halfTick * 0.5

    def expire_sequence(self, s: DtSide, ema0: float, reason: str) -> None:
        # port of DoubleTrap.ExpireSequence, lines 612-616
        s.Stage = StageIdle
        self.log_dt("DtRejected", s.IsLong, 3, ema0, None, NAN, NAN, 0, RISK_NONE, reason)

    def evaluate_candidate(self, s: DtSide, ema0: float, trend: int, inside: bool) -> None:
        # port of DoubleTrap.EvaluateCandidate, lines 618-714
        is_long, cfg, half = s.IsLong, self.cfg, self.halfTick
        reason = None
        third_entry_invalid = False
        structure_ok = False

        # Context filters
        if trend == (-1 if is_long else 1):
            reason = "trend: EMA slopes down" if is_long else "trend: EMA slopes up"

        if reason is None and trend == 0:
            crosses = self.count_ema_crosses()
            if crosses >= cfg.RangeCrossCount:
                reason = f"range filter: {crosses} EMA crosses"

        if reason is None and cfg.UseRule6Filter:
            reason = self.rule6_reason()

        if reason is None and cfg.UseExhaustionFilter and s.ExState == ExExhausted:
            reason = "exhaustion: new high after trend break" if is_long else "exhaustion: new low after trend break"

        # Condition 3: new correction extreme beyond the 1E and 2E signal bars, and bar t belongs to that leg
        if reason is None:
            lsl = "L" if is_long else "S"
            low_high = "low" if is_long else "high"
            b0 = self._bar(0)
            if cfg.DtRequireEntryStopsHit:
                # tuning DT-b: the correction extreme must reach the structural stops of the 1E and the 2E
                if s.SeqFirstStop == s.SeqFirstStop and not self._stop_hit(is_long, s.CorrExt, s.SeqFirstStop):
                    reason = f"no new {low_high} at/beyond the 1E{lsl} stop ({net_g(s.SeqFirstStop)})"
                elif not self._stop_hit(is_long, s.CorrExt, s.SecondStop):
                    reason = f"no new {low_high} at/beyond the 2E{lsl} stop ({net_g(s.SecondStop)})"
            elif s.SeqFirstExt == s.SeqFirstExt and not (s.CorrExt < s.SeqFirstExt - half if is_long
                                                         else s.CorrExt > s.SeqFirstExt + half):
                reason = f"no new {'low' if is_long else 'high'} beyond 1E{lsl} signal bar ({net_g(s.SeqFirstExt)})"
            elif not (s.CorrExt < s.SecondSigLow - half if is_long else s.CorrExt > s.SecondSigHigh + half):
                reason = f"no new {'low' if is_long else 'high'} beyond 2E{lsl} signal bar"
            if reason is not None:
                pass
            elif cfg.DtSignalBarIsCorrectionExtreme:
                # tuning DT-a: the signal bar itself is the correction extreme (equal extreme counts)
                if not (b0.low <= s.CorrExt + half if is_long else b0.high >= s.CorrExt - half):
                    reason = f"bar is not the correction {low_high} ({net_g(s.CorrExt)} @bar {self.barNumbering.get(s.CorrExtBar)})"
                elif inside:
                    reason = "inside bar"
                else:
                    structure_ok = True
            elif s.CorrExtBar != self.CurrentBar and s.CounterBreakSinceCorr:
                reason = "bar is not in the extreme leg of the correction"
            elif inside:
                reason = "inside bar"
            else:
                structure_ok = True

        b0, b1 = self._bar(0), self._bar(1)
        bar = PaBarSnapshot(Index=self.CurrentBar, Open=b0.open, High=b0.high, Low=b0.low, Close=b0.close,
                            PrevHigh=b1.high, PrevLow=b1.low, HasPrev=True, Ema=ema0)
        q = SignalBarQuality.evaluate(is_long, bar.Open, bar.High, bar.Low, bar.Close, self.signalSettings)
        risk = StopRiskCalculator.compute(is_long, bar, self.TickSize, self.riskSettings)
        dist_ticks = (bar.Low - ema0 if is_long else ema0 - bar.High) / self.TickSize

        if structure_ok:
            self.log_dt("DtCandidate", is_long, 3, ema0, q, risk.Trigger, risk.Stop, risk.RiskTicks, risk.RiskClass,
                        "bar passes context and structure")

        # Condition 4: proximity to the EMA (touching or piercing is fine)
        if reason is None and dist_ticks > cfg.DtEmaToleranceTicks + 1e-9:
            reason = f"ema distance {_f0(dist_ticks)}T > {cfg.DtEmaToleranceTicks}T"

        # Condition 5: close on the trend side of the EMA
        if reason is None and (bar.Close <= ema0 if is_long else bar.Close >= ema0):
            reason = "close not above EMA" if is_long else "close not below EMA"
            third_entry_invalid = True

        # Condition 6: Rule 5 signal bar quality
        if reason is None and not q.Ok:
            reason = "quality: " + q.Reason

        if reason is None and cfg.RequireWeak2E and not s.Weak:
            reason = "2E not weak (RequireWeak2E)"

        if reason is None and cfg.BlockTooLarge and risk.RiskClass == RISK_TOO_LARGE:
            reason = f"risk too large: {risk.RiskTicks}T"

        if reason is not None:
            if third_entry_invalid:
                if cfg.ShowInvalidThirdEntry:
                    self.draw.put(DrawText("DT_INV_" + str(self.CurrentBar), "DT x", self.CurrentBar,
                                           bar.Low if is_long else bar.High, self.label_offset(is_long, 2),
                                           cfg.InvalidColor, self.smallFontSize, False))
                self.log_dt("ThirdEntryInvalid", is_long, 3, ema0, q, risk.Trigger, risk.Stop, risk.RiskTicks,
                            risk.RiskClass, reason)
            else:
                self.log_dt("DtRejected", is_long, 3, ema0, q, risk.Trigger, risk.Stop, risk.RiskTicks,
                            risk.RiskClass, reason)
            return

        self.fire_signal(s, bar, q, risk)

    def fire_signal(self, s: DtSide, bar: PaBarSnapshot, q: SignalBarResult, risk: RiskResult) -> None:
        # port of DoubleTrap.FireSignal, lines 716-757 (Alert not ported, realtime only)
        is_long, cfg, cb = s.IsLong, self.cfg, self.CurrentBar
        two_legs = self.has_two_leg_visual(s)

        self.signal = 1 if is_long else -1
        self.entryPrice = risk.Trigger
        self.stopPrice = risk.Stop
        self.limitPrice = risk.LimitPrice if risk.RiskClass == RISK_PULLBACK_LIMIT else 0.0
        self.riskTicks = risk.RiskTicks
        self.riskClass = risk.RiskClass
        self.twoLegVisual = two_legs
        self.weak2E = s.Weak

        s.Stage = StagePending
        s.SigBar = cb
        s.SigHigh = bar.High
        s.SigLow = bar.Low
        s.Entry = risk.Trigger
        s.Stop = risk.Stop

        color = cfg.LongColor if is_long else cfg.ShortColor
        tag = "DT_" + ("L_" if is_long else "S_") + str(cb)
        text = "DT"
        if cfg.ShowDtRiskTicks:
            text += f" {risk.RiskTicks}T{StopRiskCalculator.class_suffix(risk.RiskClass)}"

        # Arrow drawn as text so it sits in the label grid.
        self.draw.put(DrawText(tag, ("▲ " if is_long else "▼ ") + text, cb, bar.Low if is_long else bar.High,
                               self.label_offset(is_long, 2), color, self.labelFontSize, True))
        self.extend_lines(s)

        reason = (f"DT {'long' if is_long else 'short'}; weak2E={'True' if s.Weak else 'False'}; "
                  f"twoLegVisual={'True' if two_legs else 'False'}; quality {q.Reason}")
        self.log_dt("DtSignal", is_long, 3, bar.Ema, q, risk.Trigger, risk.Stop, risk.RiskTicks, risk.RiskClass, reason)

    def extend_lines(self, s: DtSide) -> None:
        # port of DoubleTrap.ExtendLines, lines 759-768
        cfg, cb = self.cfg, self.CurrentBar
        if not cfg.ShowEntryStopLines:
            return
        start_ago = cb - s.SigBar + 1
        if start_ago > cb:
            return
        self.draw.put(DrawLine("DT_E_" + str(s.SigBar), cb - start_ago, s.Entry, cb, s.Entry,
                               cfg.LongColor if s.IsLong else cfg.ShortColor, False, 2))
        self.draw.put(DrawLine("DT_SL_" + str(s.SigBar), cb - start_ago, s.Stop, cb, s.Stop,
                               cfg.StopLineColor, True, 1))

    # --- Context filters (lines 772-902) ----------------------------------------------------
    def count_ema_crosses(self) -> int:
        # port of DoubleTrap.CountEmaCrosses, lines 774-786
        n = min(self.cfg.RangeLookback, self.CurrentBar - 1)
        crosses = 0
        for k in range(n):
            above = self._bar(k).close > self.ema(k)
            prev_above = self._bar(k + 1).close > self.ema(k + 1)
            if above != prev_above:
                crosses += 1
        return crosses

    def rule6_reason(self) -> str | None:
        # port of DoubleTrap.Rule6Reason, lines 788-809
        cfg = self.cfg
        n = min(cfg.Rule6Lookback, self.CurrentBar + 1)
        if n <= cfg.Rule6MinBars:
            return None
        hi, lo, sum_range = -1.7976931348623157e308, 1.7976931348623157e308, 0.0
        for k in range(n):
            bk = self._bar(k)
            hi = max(hi, bk.high)
            lo = min(lo, bk.low)
            sum_range += bk.high - bk.low
        span_ticks = (hi - lo) / self.TickSize
        if span_ticks <= cfg.Rule6MaxSpanTicks:
            return f"rule6: span {_f0(span_ticks)}T <= {cfg.Rule6MaxSpanTicks}T"
        efficiency = abs(self._bar(0).close - self._bar(n - 1).open) / sum_range * 100.0 if sum_range > 0 else 0.0
        if efficiency <= cfg.Rule6MinEfficiencyPct:
            return (f"rule6: efficiency {net_custom(efficiency, 0, 1, '.')}% <= "
                    f"{net_custom(cfg.Rule6MinEfficiencyPct, 0, 1, '.')}%")
        return None

    def update_exhaustion(self, s: DtSide, ema0: float, trend: int) -> None:
        # port of DoubleTrap.UpdateExhaustion, lines 817-863
        is_long, b0, cfg, half = s.IsLong, self._bar(0), self.cfg, self.halfTick
        trend_side = b0.close > ema0 if is_long else b0.close < ema0
        ext = b0.high if is_long else b0.low

        previous_run = s.RunLen
        previous_run_ext = s.RunExt
        if trend_side:
            s.RunExt = ext if s.RunLen == 0 else (max(s.RunExt, ext) if is_long else min(s.RunExt, ext))
            s.RunLen += 1
        else:
            s.RunLen = 0

        if trend == (-1 if is_long else 1):
            s.ExState = ExNormal
            return

        if s.ExState == ExNormal:
            if not trend_side and previous_run >= cfg.ExhaustionMinTrendBars:
                s.ExState = ExBroken
                s.BrokenExt = max(previous_run_ext, ext) if is_long else min(previous_run_ext, ext)
        elif s.ExState == ExBroken:
            if b0.high > s.BrokenExt + half if is_long else b0.low < s.BrokenExt - half:
                s.ExState = ExExhausted
                s.RunSinceEx = 1 if trend_side else 0
        elif s.ExState == ExExhausted:
            s.RunSinceEx = s.RunSinceEx + 1 if trend_side else 0
            if s.RunSinceEx >= cfg.ExhaustionMinTrendBars:
                s.ExState = ExNormal

    def has_two_leg_visual(self, s: DtSide) -> bool:
        # port of DoubleTrap.HasTwoLegVisual, lines 870-900
        is_long, half, bars = s.IsLong, self.halfTick, self.bars
        n = self.cfg.SwingStrength
        first = max(s.PullStartBar, self.CurrentBar - MaxScanBars)
        last = self.CurrentBar
        have_first_leg = False

        for i in range(first + n, last - n + 1):
            swing_low = swing_high = True
            lo, hi = bars[i].low, bars[i].high
            for k in range(1, n + 1):
                if not (lo < bars[i - k].low and lo <= bars[i + k].low):
                    swing_low = False
                if not (hi > bars[i - k].high and hi >= bars[i + k].high):
                    swing_high = False

            # Long: first leg down ends at a swing low, second leg starts at a lower swing high.
            leg_end = swing_low if is_long else swing_high
            leg_start = swing_high if is_long else swing_low
            leg_end_price = lo if is_long else hi
            leg_start_price = hi if is_long else lo

            if leg_end and (leg_end_price >= s.CorrExt - half if is_long else leg_end_price <= s.CorrExt + half):
                have_first_leg = True
            elif leg_start and have_first_leg and (leg_start_price < s.PullStartExt if is_long
                                                   else leg_start_price > s.PullStartExt):
                return True
        return False

    # --- Drawing and diagnostics (lines 904-970) -------------------------------------------
    def label_offset(self, below_low: bool, row: int) -> int:
        # port of DoubleTrap.LabelOffset, lines 906-909
        return PaLabelLayout.offset(below_low, row, self.rowHeight, self.cfg.BarNumberClearance)

    def draw_hosted_labels(self) -> None:
        # port of DoubleTrap.DrawHostedLabels, lines 911-933
        cfg, ec, cb, b0 = self.cfg, self.ec, self.CurrentBar, self._bar(0)
        le = ec.longEntryEvent
        if 0 < le <= HostedLabelMaxCount and (le > 1 or cfg.ShowFirstEntries):
            ago = cb - ec.longSignalBar
            if 0 <= ago <= cb:
                self.draw.put(DrawText("DT_ECL_" + str(cb), f"{le}EL", cb - ago, ec.longSignalLow,
                                       self.label_offset(True, 0),
                                       cfg.LongColor if ec.longSignalQualityOk else cfg.QualityFailColor,
                                       self.smallFontSize, False))
        se = ec.shortEntryEvent
        if 0 < se <= HostedLabelMaxCount and (se > 1 or cfg.ShowFirstEntries):
            ago = cb - ec.shortSignalBar
            if 0 <= ago <= cb:
                self.draw.put(DrawText("DT_ECS_" + str(cb), f"{se}ES", cb - ago, ec.shortSignalHigh,
                                       self.label_offset(False, 0),
                                       cfg.ShortColor if ec.shortSignalQualityOk else cfg.QualityFailColor,
                                       self.smallFontSize, False))
        if ec.failedSecondLong:
            self.draw.put(DrawText("DT_ECF2L_" + str(cb), "F", cb, b0.low, self.label_offset(True, 1),
                                   cfg.FailedLongColor, self.smallFontSize, False))
        if ec.failedSecondShort:
            self.draw.put(DrawText("DT_ECF2S_" + str(cb), "F", cb, b0.high, self.label_offset(False, 1),
                                   cfg.FailedShortColor, self.smallFontSize, False))

    def log_dt(self, evt: str, is_long: bool, count: int, ema_value: float, q: SignalBarResult | None,
               trigger: float, stop: float, risk_ticks_value: int, cls: int, reason: str,
               sig_high: float = NAN, sig_low: float = NAN, sig_open: float = NAN, sig_close: float = NAN,
               sig_bar_index: int = -1) -> None:
        # port of DoubleTrap.LogDt, lines 936-968
        if self.diag is None:
            return
        b0 = self._bar(0)
        explicit_signal = sig_high == sig_high
        h = sig_high if explicit_signal else b0.high
        l = sig_low if explicit_signal else b0.low
        o = sig_open if explicit_signal else b0.open
        c = sig_close if explicit_signal else b0.close
        dist = NAN if ema_value != ema_value else (l - ema_value if is_long else ema_value - h) / self.TickSize
        sig_bar_no = self.barNumbering.get(sig_bar_index if sig_bar_index >= 0 else self.CurrentBar)

        # Count column: 2 for 2E related events, 3 for DT events (technically the third entry).
        line = self.diag.format(evt, "Long" if is_long else "Short", count, sig_bar_no, h, l, o, c, ema_value, dist,
                                q.BodyPct if q is not None else NAN,
                                q.ClosePct if q is not None else NAN,
                                q.WickPct if q is not None else NAN,
                                (1 if q.Ok else 0) if q is not None else -1,
                                trigger, stop, risk_ticks_value, cls, reason)

        if self.cfg.DebugLog:
            self.printer(line)
        if self.cfg.ExportCsv:
            self.diag.write_csv(self.tradingDay, line)
            if self.diag.LastError is not None and self.cfg.DebugLog:
                self.printer("DoubleTrap CSV: " + self.diag.LastError)

