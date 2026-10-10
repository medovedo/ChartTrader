# SPEC – EntryCount (und DoubleTrap)

**Ausgangsstand 10.10.2026** = Verhalten von `EntryCount.cs` / `DoubleTrap.cs` (Stand 09.10.2026), 1:1 im
ChartTrader portiert (`entry_count.py`, `double_trap.py`) und gegen NT geprüft: EntryCount 100 % Parität
06.–09.10.2026 (siehe `PORT_NOTES.md`). Regeln haben IDs (EC-…, DT-…), auf die sich `CHANGELOG_Tuning.md` bezieht.

**Ablauf jeder Änderung:** 1. hier eintragen (Regel ändern, Datum + CHANGELOG-Nr. dazuschreiben) → 2. Python-Code /
`entrycount_doubletrap.toml` → 3. Eintrag in `CHANGELOG_Tuning.md` mit Wirkung (`pa_stats.py vorher nachher`).
Rückübertragung nach NT8 nur aus dem CHANGELOG (Schritt 4).

Tick = 0,25. „Bruch“ = mindestens 1 Tick über/unter dem Vergleichswert (intern `> Wert + halber Tick`).
Verarbeitung je abgeschlossener Bar (`Calculate.OnBarClose`), keine Vorausschau.

---

## EntryCount

### EC-1 Zwei unabhängige Zählungen
Long- und Short-Zählung laufen getrennt mit derselben Logik, gespiegelt.
- **Short**: Ursprung = tiefstes Tief; ein **Aufwärtsbein** bildet sich; eine Bar, die das Tief der letzten
  Bein-Bar (Signal-Bar) um 1 Tick unterschreitet, ist der nächste Short-Entry (1ES, 2ES, …).
- **Long**: Ursprung = höchstes Hoch; **Abwärtsbein**; Bruch des Hochs der Signal-Bar = 1EL, 2EL, …
- Zustände je Richtung: *Pullback* (warten auf ein Bein) → *Leg* (Bein läuft, Signal-Bar-Kandidat) → Entry → Pullback …

### EC-2 Bar-Klassifizierung
- **Inside-Bar**: High ≤ High der direkten Vorbar und Low ≥ Low der Vorbar (je ± halber Tick). Bei zwei Inside-Bars
  wird die zweite an der ersten gemessen.
- Nicht-Inside-Bars werden für die Bein-Erkennung an der **letzten Nicht-Inside-Bar** (Referenz-Bar) gemessen:
  Up = High > Referenz-High, Down = Low < Referenz-Low, sonst NoBreak.
- **Outside-Bar** (Up und Down): `OutsideBarPolicy` = **BothSides** (Default: beide Seiten zählen) |
  CloseDirection (nur die Seite des Close; Doji: Close ≥ Mitte = Up) | Ignore (wie Inside-Bar übersprungen).

### EC-3 Bein und Signal-Bar
- Im Pullback startet eine Bar in Bein-Richtung (Short: Up, Long: Down) ein Bein; sie ist der erste
  Signal-Bar-Kandidat.
- Läuft das Bein weiter (kein Trigger), wird die aktuelle Bar neuer Kandidat; das Bein-Extrem wird mitgeführt.
- **Inside-Bar als Signal-Bar** (`InsideBarAsSignal` = true): Eine Inside-Bar während eines Beins wird Kandidat.
  Sie ist **nie Trigger** und wird für die Bein-Erkennung übersprungen (Referenz-Bar bleibt).

### EC-4 Trigger (Entry)
- Entry, wenn eine nicht-inside Bar den Kandidaten um 1 Tick bricht (Short: Tief, Long: Hoch). Bei
  CloseDirection blockiert die nicht gezählte Seite einer Outside-Bar den Trigger.
- **`RequireEqualOrHigherLeg`** (Default **false**): Ab dem 2. Bein muss das Bein das Extrem des vorigen Beins
  erreichen (Short: Hoch ≥ voriges Bein-Hoch); sonst wird der Entry übersprungen (Event `Entry` mit Reason
  „skipped …“, Zähler steigt nicht), der Pullback läuft weiter. Steuert keinen Reset; wirkt nur indirekt (ein
  übersprungener 2E löst keine F2E-Beobachtung und damit kein R:2L aus). Ausgangswert fürs Tuning: **false**
  (Nutzer, 10.10.2026). *Die NT-Chart-Instanz 06.–09.10. lief mit true.*
- Zähler +1; Label an der **Signal-Bar** (1EL/2EL … unter dem Tief, 1ES/2ES … über dem Hoch), bis
  `MaxLabeledCount` (3); 1E-Labels per `ShowFirstEntries`.

### EC-5 Signal-Bar-Qualität (Regel 5, nur Anzeige/Info, filtert die Zählung nie)
Relativ zur Bar-Range: Richtung (Long: Close > Open), Body ≥ `MinBodyPct` (45), Close-Position ≥ `MinClosePct` (70,
Long vom Low gemessen), Ausbruchsdocht ≤ `MaxBreakoutWickPct` (25, Long: oberer Docht).
Ausnahme Rejection-Bar (`UseRejectionException`): Close ≥ 90 % und Docht ≤ 10 % → ok, Body und Richtung egal.
Label grau (`QualityFailColor`), wenn nicht ok.

### EC-6 Trigger, Stop, Risiko (nur Info)
Trigger = Signal-Bar-Hoch + 1 Tick (Long) / -Tief − 1 Tick (Short). Stop (`StopMode` SignalAndPrevBar) = jenseits des
Extrems von Signal-Bar und deren Vorbar ± 1 Tick. Risiko in Ticks (Math.Round). Max Risk = `RiskTemplateTicks` − 1
(16 → 15). Klasse: Direct (Risiko ≤ 15 + `RiskToleranceTicks` 2), PullbackLimit (fehlende Ticks ≤ 49 % der
Struktur; Limit = Stop ± 15 Ticks), sonst TooLarge.

### EC-7 Gescheiterter Second Entry (F2E)
- Nach einem 2E wird beobachtet (unabhängig von späteren Resets der Zählung); ein 3E beendet die Beobachtung.
- **`FailedEntryMode` PriorBarBreak** (Default): F2E, sobald eine spätere nicht-inside Bar die **direkte Vorbar**
  um 1 Tick in Gegenrichtung bricht (Long: Tief der Vorbar, auch wenn diese eine Inside-Bar ist).
  SignalBarBreak: erst beim Bruch des Extrems der 2E-Signal-Bar.
- **Gleiche Bar**: Eine Outside-Bar, die den 2E auslöst und zugleich das Gegen-Extrem der Signal-Bar bricht, ist
  Entry und F2E zugleich – nur wenn die Entry-Seite zuerst gehandelt wurde. Reihenfolge aus der 1-Tick-Serie;
  ohne Ticks Bar-Farbe (rot: erst Hoch, grün: erst Tief, Doji: Extrem näher am Open zuerst).
- Anzeige: „F“ an der F2E-Bar (grün F2EL unter dem Tief, rot F2ES über dem Hoch).

### EC-8 Resets der Zählung
- **a/c Key Level** (`ResetOnKeyLevel`): Kontakt mit EMA (± `KeyLevelToleranceTicks` 2), optional Swing-S/R und
  Trendlinie. `ResetScope` CounterTrendOnly: nur die Zählung gegen den EMA-Trend (Aufwärtstrend → Short-Zählung);
  flache EMA → kein Reset. Nur bei **neuem** Kontakt (Vorbar ohne Kontakt). Läuft **vor** der Zählung der Bar und
  behält das laufende Bein: ein Entry auf der Kontakt-Bar ist der 1E der neuen Zählung. Wartet die Zählung bei
  Kontakt noch am Ursprung, wird nur der Ursprung verschoben.
- **b Zwei Beine** (`ResetAfterTwoLegs`): nach einem F2E → Zähler 0, neuer Ursprung = Extrem seit dem 2E-Trigger.
- **d Neues Extrem** (`ResetOnNewExtreme`): Bruch über den Ursprung (Long: Hoch > Ursprungs-Hoch) → Zähler 0,
  neuer Ursprung = diese Bar; F2E-Beobachtung endet.
- Anzeige: Punkt am neuen Ursprung + R:KL / R:2L / R:NX.

### EC-9 Trend und Swings
- Trend: EMA(`EmaPeriod` 21) heute minus vor `EmaSlopeLookback` (5) Bars, in Ticks: ≥ `MinEmaSlopeTicks` (2) → +1,
  ≤ −2 → −1, sonst 0.
- Swings (für S/R, Trendlinien): `SwingStrength` (2) Bars je Seite, links strikt, rechts gleich erlaubt.

### EC-10 Ausgabe
Ereignis-CSV wie NT (36 Spalten, Bar-Zeilen), Bar-Nummern beginnen je Kalendertag bei 1.

### Parameter (Ausgangswerte = C#-Defaults, `entrycount_doubletrap.toml`)
EmaPeriod 21 · KeyLevelToleranceTicks 2 · SwingStrength 2 · UseSwingSR false · SrLookbackSwings 5 ·
SrToleranceTicks 2 · UseTrendlines false · OutsideBarPolicy BothSides · EmaSlopeLookback 5 · MinEmaSlopeTicks 2 ·
MinBodyPct 45 · MinClosePct 70 · MaxBreakoutWickPct 25 · UseRejectionException true · RejectionMinClosePct 90 ·
RejectionMaxWickPct 10 · StopMode SignalAndPrevBar · RiskTemplateTicks 16 · RiskToleranceTicks 2 · MaxPullbackPct 49 ·
RequireEqualOrHigherLeg **false** (entschieden 10.10.2026; der NT-Chart lief mit true) · ResetOnKeyLevel true ·
ResetScope CounterTrendOnly · ResetAfterTwoLegs true · ResetOnNewExtreme true · FailedEntryMode PriorBarBreak ·
InsideBarAsSignal true

### Offene Punkte aus PORT_NOTES (nicht umgesetzt, nur zur Entscheidung)
#1 Parameter in die CSV · #4 Ursprungs-Vergleich ohne halben Tick · #5 übersprungener Entry als eigenes Event.

---

## DoubleTrap
Noch nicht getunt, Parität offen (keine NT-CSV). Verhalten = `DoubleTrap.cs`, Regeln werden vor dem DT-Tuning
hier ergänzt.
