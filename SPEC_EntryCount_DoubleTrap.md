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
  **Im CT aus** (`ResetOnKeyLevel = false`, CHANGELOG Nr. 6, Nutzer 11.10.2026): „Hat die Long-Zählung ein Hoch über
  der EMA als Ursprung und läuft der Kurs darunter, wird weiter auf EL gezählt bis zum F2EL oder bis zu einem Hoch
  über dem Ursprung; Short gespiegelt.“ Als eigene Bedingung (kein Reset, solange der Ursprung jenseits der EMA
  liegt) gemessen, entfiel damit jeder Key-Level-Reset (60 an 06.–09.10.2026, 49 an 20./21.03.2025; CSVs
  byte-gleich mit `false`): der Ursprung liegt bei einem EMA-Kontakt praktisch immer jenseits der EMA.
  Beispiel 21.03.2025: 1EL Bar 110, EMA-Kontakt Bar 111 setzte die Long-Zählung zurück → Bar 120 war 1EL statt 2EL.
- **b Zwei Beine** (`ResetAfterTwoLegs`): nach einem F2E → Zähler 0, neuer Ursprung = Extrem seit dem 2E-Trigger.
- **d Neues Extrem** (`ResetOnNewExtreme`): Bruch über den Ursprung (Long: Hoch > Ursprungs-Hoch) → Zähler 0,
  neuer Ursprung = diese Bar; F2E-Beobachtung endet.
- Anzeige: Punkt am neuen Ursprung + R:KL / R:2L / R:NX.

### EC-11 Pause der Gegentrend-Zählung nach ihrem F2E (neu, CHANGELOG Nr. 1, 10.10.2026)
Parameter `PauseCountAfterCounterTrendF2E`. Im CT **eingeschaltet** (`entrycount_doubletrap.toml`, CHANGELOG Nr. 2);
Code-Default false = Verhalten des C#-Codes (Parität).
- **Beginn**: Ein F2ES auf einer Bar mit Trend +1 (EC-9, Aufwärtstrend) pausiert die **Short**-Zählung;
  ein F2EL bei Trend −1 (Abwärtstrend) pausiert die **Long**-Zählung. Die Pause beginnt nach dem Reset b
  (R:2L) derselben Bar; das laufende Bein wird verworfen.
- **Während der Pause** ist die Richtung eingefroren: keine Beine, keine Kandidaten (auch keine Inside-Bars),
  keine Entries, keine Labels, keine Resets a/c/d. Die andere Richtung zählt normal weiter.
- **Ende**: die erste Bar, deren **Close unter der EMA** liegt (Short-Pause) bzw. **über der EMA** (Long-Pause).
  Auf dieser Bar startet die Zählung neu wie bei der ersten Bar (Zähler 0, kein Bein, Ursprung = Tief bzw. Hoch
  dieser Bar) und die Bar wird schon normal gezählt (kann ein Bein starten).
- CSV: Events `CountPaused` und `CountResumed` (Reason mit Grund bzw. Close/EMA und neuem Ursprung); in der
  Bar-Zeile steht der Zustand „Paused“. Ist der Parameter aus, ändert sich an der CSV nichts.
- Gilt nur für EntryCount; das von DoubleTrap gehostete EntryCount bleibt beim C#-Verhalten (DT wird später getunt).

### EC-12 F2E-Beobachtung bis zum Ursprung des 2E (neu, CHANGELOG Nr. 5, 11.10.2026)
Parameter `F2EWatchUntil2EOrigin`. Im CT **eingeschaltet** (`entrycount_doubletrap.toml`); Code-Default false = C#-Verhalten.
- C#: Reset d (neues Extrem) beendet die F2E-Beobachtung bei **jedem** Bruch des aktuellen Ursprungs. Hat ein
  Key-Level-Reset (a/c) nach dem 2E den Ursprung auf die Kontakt-Bar verlegt, reicht schon das nächste höhere Hoch
  (Long) – der 2E gilt als erfolgreich, obwohl der Pullback-Start nicht erreicht ist.
- Neu: Beim 2E wird der Ursprung seiner Zählung gemerkt (`F2OriginPrice`). Reset d beendet die Beobachtung nur noch,
  wenn die Bar **diesen** Ursprung bricht (Long: Hoch > Ursprung des 2E). Zähler und Ursprung setzt Reset d wie bisher zurück.
- Beispiel 19.03.2025 (ES JUN25, 2000 Tick, Bar-Nr. des Indikators): 2EL Bar 87, Ursprung 5692.50 (Bar 73); Key Level
  Bar 88 → Ursprung 5682.75; Bar 89 Hoch 5687.25 beendete die Beobachtung. Mit EC-12: F2EL an Bar 92 (Tief unter Bar 91,
  Hoch 5690.50 < 5692.50).
- Gilt nur für EntryCount; das von DoubleTrap gehostete EntryCount bleibt beim C#-Verhalten.

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
RequireEqualOrHigherLeg **false** (entschieden 10.10.2026; der NT-Chart lief mit true) · ResetOnKeyLevel true (**im CT false**, CHANGELOG Nr. 6) ·
ResetScope CounterTrendOnly · ResetAfterTwoLegs true · ResetOnNewExtreme true · FailedEntryMode PriorBarBreak ·
InsideBarAsSignal true · PauseCountAfterCounterTrendF2E **true** im CT (neu, EC-11; Code-Default false) ·
F2EWatchUntil2EOrigin **true** im CT (neu, EC-12; Code-Default false)

### Offene Punkte aus PORT_NOTES (nicht umgesetzt, nur zur Entscheidung)
#1 Parameter in die CSV · #4 Ursprungs-Vergleich ohne halben Tick · #5 übersprungener Entry als eigenes Event.

---

## DoubleTrap

**Vorgehen (Nutzer, 10.10.2026):** DT wird im CT getunt und danach nach C# übertragen. Die NT-Parität des DT-Ports
wurde nicht geprüft (keine NT-CSV); abgesichert durch die Testfälle 10–15. Ausgangsstand = `DoubleTrap.cs`.
DoubleTrap hostet ein eigenes EntryCount mit den DT-Parametern (EC-11 dort nicht aktiv).

### Ziel: Regeln nach Thomas Wade
- **W1 Kontext**: Der Markt spricht insgesamt noch für den Haupttrend.
- **W2 Erster Trap**: Ein Second Entry mit dem Trend (2EL/2ES) bildet sich und scheitert an schlechten Bedingungen
  (z. B. weit weg von der EMA, in Kongestion, schwache Signal-Bar, blockiert durch eine aktive kurzfristige Trendlinie).
- **W3 Zweiter Trap**: Gegentrend-Trader werden gefangen, die den gescheiterten 2E (F2EL/F2ES) gegen den Trend handeln.
- **W4 Signal-Bar**: nah an der EMA (nicht weit weg). Bullischer DT: Close **über** der EMA; bärischer: **unter**.
- **W5 Strukturelles Extrem**: Die neue Signal-Bar stoppt den ersten und zweiten Trend-Entry aus und ist das
  absolute Tief (Long) bzw. Hoch (Short) der laufenden Korrektur.

### Ausgangsstand (C#), Long beschrieben, Short gespiegelt
- **DT-1 Ablauf je Richtung**: 2EL (rohes Ereignis, unabhängig von Resets) → warten auf F2EL → Signal-Bar suchen
  (auch die F2E-Bar selbst, `DtAllowF2EBarAsSignal`) → Signal: Entry = Hoch + 1 Tick, wartet → ausgelöst, sobald ein
  Hoch den Entry erreicht (Raute), oder verworfen bei einem neuen Korrektur-Tief unter der Signal-Bar (dann
  wieder Signal-Bar suchen). F2E auf der 2E-Bar selbst (Outside-Bar) zählt sofort.
- **DT-2 Start**: Der 2EL muss **mit dem Trend** sein (EMA-Trend +1 auf der 2E-Bar), sonst `DtRejected` „not with
  trend“. Der 1EL derselben Zählung (zwischen Pullback-Start und 2E) liefert das 1E-Signal-Bar-Tief.
- **DT-3 Weak 2E** (nur Info, Label DT?, `RequireWeak2E` = false): a) 2E-Signal-Bar > `EmaGapTicks` (4) über der
  EMA; b) Kongestion: mittlere Überlappung der letzten `CongestionLookback` (6) Bars ≥ 60 % oder Spanne < 12 Ticks;
  c) Signal-Bar besteht Regel 5 nicht; d) Gegenstruktur intakt: seit dem höchsten Hoch zwischen 1E-Trigger und
  2E-Signal-Bar kein Close darüber.
- **DT-4 Verfall**: neues Hoch über dem Pullback-Start (Ursprung der Zählung) oder mehr als `DtMaxBarsAfterF2E`
  (10) Bars nach dem F2E ohne Signal.
- **DT-5 Korrektur-Tief**: tiefstes Tief seit dem 2E-Trigger; „Gegenbewegung“, sobald danach eine Bar das Hoch der
  Vorbar bricht.
- **DT-6 Prüfung der Signal-Bar** (Reihenfolge): EMA fällt → nein · EMA flach und ≥ `RangeCrossCount` (4) Closes über/
  unter der EMA in `RangeLookback` (30) Bars → nein (Range) · Regel 6 (aus) · Exhaustion (Trendbruch + neues Hoch) →
  nein · Korrektur-Tief unter dem 1E- **und** 2E-Signal-Bar-Tief · Bar im Extrem-Bein (ist das Tief oder keine
  Gegenbewegung seither) · keine Inside-Bar · Tief höchstens `DtEmaToleranceTicks` (2) über der EMA · Close über der
  EMA (sonst „DT x“ / ThirdEntryInvalid) · Regel 5 ok · optional RequireWeak2E, BlockTooLarge.
- **DT-7 Signal**: ▲ DT unter der Bar (Zeile 2), Entry-/Stop-Linie bis zur Auslösung, Stop = Struktur-Stop
  (Signal-Bar und Vorbar − 1 Tick), Risiko-Klasse wie EC-6.

### Abgleich mit Wade (Stand 10.10.2026)
| Wade | Code | Lücke | Vorschlag |
|---|---|---|---|
| W1 | DT-2, DT-6 (Trend, Range, Exhaustion) | – | – |
| W2 | DT-3 nur Info | schlechte Bedingungen nicht Pflicht; keine echte Trendlinie | **DT-c** RequireWeak2E; **DT-d** Trendlinien-Kriterium |
| W3 | DT-1 | Trend beim F2E nicht geprüft | **DT-e** F2E noch im Trend |
| W4 | DT-6 (EMA-Nähe, Close) | – | – |
| W5 | DT-6 (unter Signal-Bar-Tiefs, Extrem-Bein) | Signal-Bar muss nicht selbst das Tief sein; geprüft gegen Signal-Bar-Tiefs statt Stops | **DT-a** Signal-Bar = Korrektur-Tief; **DT-b** unter die Struktur-Stops von 1E/2E |
| – | Regel 5 der Signal-Bar, Verfall nach 10 Bars | zusätzlich zu Wade | offen |

### DT-a Signal-Bar ist das Korrektur-Extrem (neu, CHANGELOG Nr. 3, 10.10.2026)
Parameter `DtSignalBarIsCorrectionExtreme` (DoubleTrap, Default false = C#). Eingeschaltet ersetzt er in DT-6 die
Bedingung „Bar im Extrem-Bein“: Das Tief der Signal-Bar muss das Korrektur-Tief seit dem 2E-Trigger sein
(gleich tief zählt, ± halber Tick); Short: Hoch = Korrektur-Hoch. Sonst `DtRejected` „bar is not the correction low“.

### DT-b 1E und 2E ausgestoppt (neu, CHANGELOG Nr. 4, 10.10.2026)
Parameter `DtRequireEntryStopsHit` (DoubleTrap, Default false = C#). Eingeschaltet wird in DT-6 das Korrektur-Tief
nicht mit den Signal-Bar-Tiefs von 1E/2E verglichen, sondern mit deren Struktur-Stops (EntryCount `LongStop`/
`ShortStop` beim jeweiligen Entry, EC-6): Korrektur-Tief ≤ 1E-Stop (wenn der 1E zur Folge gehört) und ≤ 2E-Stop;
Short: Korrektur-Hoch ≥ Stops. Sonst `DtRejected` „no new low at/below the 1EL/2EL stop (Preis)“.

### Ausgangszahlen DT (06.–09.10.2026, NT-Bars + NT-Ticks, `pa_csv/ec11_pause`)
2 Signale in 4 Tagen (1 Long am 07.10., ausgelöst; 1 Short am 08.10., nicht ausgelöst). Häufigste Ablehnung:
2E nicht mit dem Trend (Long 68, Short 77), danach Range-Filter (19/5), Verfall durch neues Extrem (16/11),
Trend gegen die Richtung an der Signal-Bar (6/12), kein neues Extrem unter dem 2E (3/10).
