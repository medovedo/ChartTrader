# ChartTrader – Logs auswerten (Kontext für die Trade-Analyse)

## Was der ChartTrader ist
Tick-Replay für ES-Futures (Databento-Daten, meist MBO = ein Tick je Fill wie in NinjaTrader).
Ich handle im Replay manuell oder mit dem "Sniper" (Port meiner NT-Strategie LotsenhofSniper,
ATM-Vorlagen WADES6..16, Max Risk = Zahl im Namen − 1 Ticks). Fills, OCO, Positionen und PnL
kommen aus der NautilusTrader-SimulatedExchange. ES: 1 Tick = 0,25 Punkte = 12,50 $, 1 Punkt = 50 $.
Chart meist 2000-Tick-Bars (tick:2000), EMA 21. Regel: nach einem Verlust-Trade ist der Tag beendet.

## Wichtig beim Lesen des Logs
- Die Log-Zeilen unter dem Chart haben **keine Uhrzeit**; es zählt die Reihenfolge.
  Uhrzeiten, Dauer und PnL stehen im Trade-Log (CSV, siehe unten).
- Preise mit Punkt (5967.00), Trade-Log mit Dezimalkomma (5967,00).
- Mehrere `FILL`-Zeilen = ein Auftrag in mehreren Teilen oder mehrere Brackets (eine je ATM-Bracket).
- Bar-Nummern im Log (`bar 50`, `bars 40..59`) sind die Nummern wie im Chart: je Session ab 1,
  Session-Start 17:00 Chicago (00:00 Berlin).

## Meldungen
**Replay/Chart**
- `Jumped to 2025-01-02 15:30 CET (tick N)` – Sprung zu Datum/Uhrzeit; Start der Session-Auswertung.
- `Order moved A -> B (effective on the next tick)` – **ich** habe eine Stop-/Target-Linie von Hand gezogen.
- `Text '…' at P (bar N)`, `Trend line A -> B (bars N..M)` – meine Markierungen im Chart; der Text
  sagt oft, was ich gesehen habe (z. B. `new high`, `OS`).

**Exchange (Nautilus)**
- `FILL BUY|SELL <Menge> @ <Preis>` – Ausführung. SELL beim Short-Einstieg bzw. Long-Ausstieg.
- `REJECTED <Grund>` – Order abgelehnt.
- `MODIFY REJECTED <Grund>` – Änderung abgelehnt, z. B. Stop auf der falschen Marktseite
  („was in the market“). Die Order bleibt wie sie war.
- `JOURNAL SHORT 3 5967 -> 5956  44 ticks  PnL 1650  (Smart Short WADES16)` – Trade abgeschlossen
  (flat): Richtung, Kontrakte, Einstieg, **Durchschnitts**-Ausstieg, Ticks, PnL in $, Setup.

**Sniper** (alle mit Präfix `SNIPER`)
- `ATM switched to WADES16, max risk 15 ticks` – gewählte Vorlage.
- `Smart Long|Short Trap Armed. Trigger: T, Limit: L, SL: S, ATM: … (Risk OK. Direct trigger entry.)` –
  Einstieg direkt am Trigger (1 Tick über/unter der Signal-Bar).
- `… (pullback entry, risk R > M ticks)` – Risiko bis Struktur-Stop zu groß → Limit als Pullback
  auf Stop ± Max Risk (füllt nur bei Rücklauf).
- `Smart …: Limit 3 @ L (WADES16), intended SL S` – Entry-Orders liegen im Markt.
- `… trap expired (bar closed without trigger)` – Trigger in der Bar nicht erreicht, verfallen.
- `… EXPIRED: price ran N ticks past trigger` / `allowed bars exceeded` – ungefüllte Entry storniert.
- `… filled @ P` – Einstiegspreis. `STRUCTURAL SL: secured at S` – Stops auf den Struktur-Stop gesetzt.
- `PARTIAL FILL: rest of entry cancelled (10 ticks run)` – Teilfill, Rest storniert, weil der Kurs seit
  dem Fill 10 Ticks gelaufen ist; `Bracket|Runner N contract(s): new stop S, target T` – neue Exits dafür.
- `AUTO-BE: Stop2 moved to P` – Target1 gefüllt → Runner-Stop auf Einstand.
- `TRAIL: Stop2 moved to P` – Runner-Stop hinter bestätigten Swing (2 Bars je Seite, ≥ 8 Ticks, +1 Tick).
- `SCRATCH: …` – Targets auf Einstand + Offset gezogen. `Killswitch activated …` – Cancel All.
- `… closed.` – Trade beendet.
- `Loss registered: -X (n/1).` + `TRADING LOCKED for <Tag> …` – Verlust-Trade, Tagessperre aktiv.
- `WARNING …: N contract(s) open, only M covered by a stop!` – **kritisch**: ungeschützte Kontrakte.
- `ERROR: …` / `Signal bar rejected: close 50.0% < 70% | …` / `Inside Bar blocked` /
  `Double bar structure too large` – Setup abgelehnt, keine Order.

## Typischer Ablauf eines Trades
Trap Armed → Limit … @ L → FILL … → filled @ → STRUCTURAL SL → (Order moved = mein Eingriff)
→ FILL (Target1) → AUTO-BE → TRAIL … → FILL (Runner) → JOURNAL … → closed.

## Trade-Log (journal/trade_log.csv, Excel: `;` und Dezimalkomma)
Eine Zeile je Trade von flat zu flat (ATM mit Target1 + Runner = eine Zeile, Exit = Durchschnitt).
Spalten: `Replayed` (wann ich den Replay gespielt habe) · `Date` · `Open` · `Close` (Berlin-Zeit) ·
`Instrument` · `Setup` (`Manual Market`, `Manual Bracket` oder z. B. `Smart Short WADES16`) ·
`Side` · `Qty` · `Entry` · `Exit` · `Points` · `Ticks` · `PnL` ($) · `Duration`.

## Bekannte Eigenheiten der Simulation
- Limits füllen, sobald ein Trade den Preis erreicht (keine Queue-Position) → eher optimistisch.
- Stops füllen als Market zum nächsten Trade, auch mit Slippage (z. B. 5980.75 und 5981.00).
- Ein `MODIFY REJECTED … in the market` für Stop1 genau beim Stop-Fill ist harmlos.

## Bitte bei der Analyse
Prüfe je Trade: Setup-Qualität (Signal-Bar, direkt vs. Pullback, Risiko in Ticks), ob das
Handelsfenster 15:00–17:30 Berlin eingehalten wurde, meine manuellen Eingriffe (`Order moved`) im
Vergleich zu dem, was Sniper/Trail getan hätten, Exit-Grund und Regel-Einhaltung (nach Verlust Schluss).
Ich liefere: Log-Auszug, die Zeilen aus trade_log.csv und ggf. einen Chart-Screenshot.
