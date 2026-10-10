# ChartTrader – alle deterministischen Regeln

Stand 10.10.2026. Beschreibt, was der ChartTrader (CT) bei gleichen Daten und gleichen Klicks immer gleich tut:
Bar-Bau, Order-Ausführung, Sniper (Port von `LotsenhofSniper.cs`), ATM-Vorlagen, Trade-Log und die Indikatoren
EntryCount/DoubleTrap. Quelle ist der Code; bei Widerspruch gilt der Code. Ausführliche Indikator-Regeln mit
Änderungshistorie: `SPEC_EntryCount_DoubleTrap.md`, `CHANGELOG_Tuning.md`.

Konventionen: ES, Tick = 0,25 Punkte = 12,50 $. Bar[0] = laufende Bar, Bar[1] = letzte geschlossene Bar
(= Signal-Bar), Bar[2] = die davor. Long beschrieben, Short jeweils gespiegelt.

---

## 1. Daten und Wiedergabe

- **Ticks**: Databento MBO-Fills (Action F, 1 Fill = 1 NT-Tick) oder NTs eigener Tick-Export (`*.Last.txt`,
  Aggressor-Seite aus Bid/Ask). Das Databento-Trades-Schema ergibt nur ~1/3 der NT-Bars und wird für 2000-Tick-Bars
  nicht verwendet.
- **Handelstag** = CME-Session ab 17:00 Chicago (zählt zum nächsten Kalendertag).
- **Kontext**: Beim Start mit `--day` werden die Ticks des Vortags nur durch den Bar-Bau und die Indikatoren
  geschickt (keine Orders), damit Bars und EMA dieselbe Phase haben wie in NT.
- **Uhr**: Wiedergabe zeitgetreu in Datenzeit × Faktor (0,5 / 1 / 2 / 5 / 10 / 20 / 50 / 100). Alle 40 ms werden
  alle Ticks bis zur simulierten Uhr gespielt (höchstens 5000 je Frame). Pausen ohne Trades werden auf 3 s gekürzt.
- **Sprung** („Springe zu“): nur vorwärts und nur flat. Alle Ticks bis zum Ziel laufen nur durch Bar-Bau und
  Indikatoren, nicht durch die Exchange.

## 2. Bar-Bau

- **Tick-Bars** (`tick:N`, Standard `tick:2000`): Eine Bar schließt nach N Trades; die nächste öffnet mit dem
  folgenden Trade. Aus NTs eigenen Ticks identisch mit NTs Bars (1736/1736 geprüft).
- **Range-Bars** (`range:N`): High − Low = genau N Ticks. Verlässt ein Trade die Spanne, schließt die Bar an der
  Kante (Close = Kante); die nächste öffnet am Close der vorigen (keine Phantom-Lücke). Bei einem Sprung über
  mehrere Spannen entstehen mehrere Bars im selben Tick.
- **Session-Wechsel** (17:00 Chicago): Die laufende Bar wird geschlossen, auch wenn sie nicht voll ist; die
  Bar-Nummerierung beginnt bei 1.
- **EMA** (Anzeige und Indikatoren): EMA 21 auf Bar-Closes wie NT: erster Wert = erster Close, danach
  `Close · 2/(n+1) + Vorwert · (1 − 2/(n+1))`.

## 3. Order-Ausführung (Nautilus SimulatedExchange, keine eigene Fill-Logik)

- Konto NETTING, Margin. Orders der GUI und des Sniper gehen an die SimulatedExchange von NautilusTrader.
- **Limit** füllt zum Limitpreis, sobald ein Trade auf diesem Preis handelt, mit dem Volumen des Trades.
- **Stop-Market** löst aus, wenn ein Trade den Stop erreicht, und füllt zum Marktpreis (Bid/Ask aus den
  Aggressor-Seiten der Trades).
- **Bracket** = Entry + Stop + Target. Stop und Target hängen per OTO am Entry und sind untereinander OCO:
  Füllt eines, wird das andere storniert. Wird ein Entry storniert oder seine Menge reduziert, storniert die
  Exchange auch dessen Stop/Target.
- **Zeitpunkt**: Eine Order, die in einem Tick abgeschickt oder geändert wird, wirkt bei der Exchange erst mit dem
  nächsten Tick. Ein Storno greift auch für Orders, die die Exchange noch nicht angenommen hat (seit 10.10.2026).
- **Stop verschieben**: Liegt der neue Stop schon im Markt (Long-Stop ≥ Bid), lehnt die Exchange ab
  (`MODIFY REJECTED … in the market`); der alte Stop bleibt.
- In der GUI ziehbar sind nur offene Orders mit Parent, also Stop/Target eines gefüllten Brackets.

## 4. Manuelle Orders (GUI)

- Market, Limit und Stop-Market mit der Menge aus dem Feld.
- **Manual Bracket**: Market-Entry; Target = letzter Preis ± Target-Ticks, Stop = letzter Preis ∓ Stop-Ticks.
- **Flatten**: alle Orders stornieren, Position glattstellen.
- Eine Tagessperre nach einem Verlust gibt es im CT nicht (bewusst anders als die NT-Strategie).

## 5. Sniper (Port LotsenhofSniper)

### 5.1 Parameter (Defaults)
| Parameter | Wert |
|---|---|
| ATM-Vorlagen (Reihenfolge beim Durchschalten) | WADES12, WADES10, WADES14, WADES16, WADES8, WADES6 |
| Max Risk | erste Zahl im Vorlagennamen − 1 (WADES12 → 11 Ticks); ohne Zahl 9 |
| Risk Tolerance | 2 Ticks |
| Max Pullback | 49 % |
| Inside-Bars blockieren | ja |
| Max Slippage (Momentum) | 2 Ticks |
| Max Runway | 10 Ticks |
| Bars to Wait | 3 |
| Teilfill-Storno | 10 Ticks |
| Signal-Bar-Filter | Close-Position ≥ 70 %, Body ≥ 5 %, Ausbruchsdocht ≤ 25 % |
| Auto-Breakeven | ja, Offset 0 |
| Runner-Trail | ja, Swing-Stärke 2, min. Swing 8 Ticks, Puffer 1 Tick |
| Scratch-Offset | 2 Ticks |
| No-Runner-Suffix | NR |

### 5.2 Gemeinsame Prüfungen beim Armen (Smart, Deep, Momentum)
1. Es braucht mindestens 2 geschlossene Bars.
2. **Inside-Bar**: Ist die Signal-Bar eine Inside-Bar (High ≤ Bar[2]-High und Low ≥ Bar[2]-Low), wird
   abgelehnt: „Inside Bar blocked“.
3. **Signal-Bar-Filter** (Range = High − Low; Range 0 → abgelehnt):
   - Close-Position = (Close − Low) / Range ≥ 70 %.
   - Body = |Close − Open| / Range ≥ 5 %.
   - Ausbruchsdocht = (High − Close) / Range ≤ 25 %.
   - Ist die Bar „rasiert“ (Close auf dem High, ± halber Tick), entfallen Body- und Docht-Prüfung.

### 5.3 Smart Long (Trap)
- Stop = min(Low Bar[1], Low Bar[2]) − 1 Tick.
- Trigger = High Bar[1] + 1 Tick.
- Risiko = (Trigger − Stop) in Ticks.
- Struktur = (High Bar[1] − min(Low Bar[1], Low Bar[2])) in Ticks.
- **Direkt**, wenn Risiko ≤ Max Risk + Toleranz: Limit = Trigger.
- **Pullback-Limit** sonst:
  - Benötigt = Risiko − Max Risk.
  - Erlaubt = abgerundet(Struktur · 49 %).
  - Ist Benötigt > Erlaubt, wird abgelehnt: „Double bar structure too large“.
  - Sonst Limit = Stop + Max Risk Ticks.
- Der **Trap** lebt nur in der Bar, in der er geschärft wurde. Schließt sie ohne Trigger, verfällt er
  („trap expired“).
- Erreicht das High von Bar[0] den Trigger, feuert der Trap: Je ATM-Bracket wird eine Bracket-Order mit Entry =
  Limit gelegt. Stop/Target sitzen zunächst relativ zum Limit (Vorlagen-Ticks).
- Das gilt auch, wenn Bar[0] schon vor dem Klick über dem Trigger lag: Dann feuert der Trap im nächsten Tick und
  verfällt sofort durch die Runway-Regel (5.6).

### 5.4 Deep Long (Trap)
- Stop wie Smart. Kerze = Range Bar[1] in Ticks; Pullback = abgerundet(Kerze · 49 %).
- Limit = High Bar[1] − Pullback Ticks.
- Ist das Risiko (Limit − Stop) > Max Risk, gilt Limit = Stop + Max Risk Ticks.
- Trigger, Trap und Verfall wie Smart.

### 5.5 Momentum Long (sofort, ohne Trap)
- Entry = High Bar[1] + 1 Tick; Limit = Entry + 2 Ticks.
- Stop = Low Bar[1] − 1 Tick, höchstens Max Risk Ticks unter dem Entry.
- Liegt der Close von Bar[0] schon ≥ Entry, wird eine Limit-Order (zum Limit) gelegt, sonst eine Stop-Limit-Order
  (Stop = Entry, Limit = Entry + 2).

### 5.6 Verfall einer ungefüllten Entry
- **Runway**: (High Bar[0] − Trigger) ≥ 10 Ticks → „EXPIRED: price ran 10 ticks past trigger“, Entry storniert.
- **Bars to Wait**: aktuelle Bar ≥ Platzierungs-Bar + 3 → „EXPIRED: allowed bars exceeded“, Entry storniert.
- Der Trade bleibt in der Verwaltung, bis die Exchange den Storno bestätigt. Kommt vorher ein Fill, wird er normal
  verwaltet („filled @ … (after the cancel request)“).

### 5.7 Nach dem Fill
- **Struktur-Stop**: Stop1 wird sofort nach dem ersten Fill auf den beabsichtigten Stop (Setup-Stop) gesetzt.
  Der Runner-Stop folgt erst, wenn alle Entries abgeschlossen sind.
- **Targets** werden einmal auf den tatsächlichen Fill-Preis ausgerichtet: Fill ± Target-Ticks der Vorlage.
- **Menge**: Stop und Target folgen der gefüllten Menge ihres Entries.
- **Teilfill**: Ein Rest-Storno erfolgt, wenn der letzte Preis seit dem Fill 10 Ticks im Gewinn war. Der Weg vor
  dem Fill zählt nicht. Der Rest der Entry wird storniert, und die gefüllten Kontrakte bekommen sofort ein neues
  OCO-Paar (reduce-only) mit dem bereits gesetzten Stop/Target.
- **ATM-Breakeven der Vorlage** (falls in der XML gesetzt): ab Gewinn ≥ Trigger-Ticks (gemessen am High von
  Bar[0]) Stop auf Entry + Plus-Ticks. Die WADES-Vorlagen haben das nicht.
- **Auto-Breakeven**: Ist Target1 gefüllt, wird der Runner-Stop auf Entry + 0 gezogen (nur enger, nie weiter).
- **Runner-Trail**: Er läuft ab dem Fill von Target1, einmal je neuer Bar.
  - Gesucht wird der jüngste bestätigte Swing-Low nach der Setup-Bar. Bestätigt heißt: 2 Bars je Seite, die
    neuere Seite strikt höher, die ältere Seite ≥.
  - Prominenz: Höchstes High seit dem Pivot − Pivot ≥ 8 Ticks.
  - Kandidat = Pivot − 1 Tick.
  - Er wird nur übernommen, wenn er enger ist als der aktuelle Runner-Stop und unter dem Close von Bar[0] liegt.
  - Lookback höchstens 100 Bars.
- **Manuell gezogene Stops/Targets** werden übernommen. Der Sniper zieht danach nur noch enger und überschreibt
  sie nicht mit dem Struktur-Stop oder der Target-Ausrichtung.
- **Warnung**, wenn offene Kontrakte nicht vollständig durch Stops gedeckt sind.

### 5.8 Weitere Befehle
- **Scratch**: Die Targets aller gefüllten Trades gehen auf Entry ± 2 Ticks, nur wenn das näher ist. Der Runner
  wird erst nach abgeschlossener Entry angepasst.
- **Cancel All** (Killswitch): Trap löschen, Entries stornieren, alle Positionen schließen.
- **No Runner**: Smart mit der Vorlage „<Name>NR“ (ein Bracket, 3 Kontrakte).

## 6. ATM-Vorlagen

Die Vorlagen werden aus `Documents\NinjaTrader 8\templates\AtmStrategy\<Name>.xml` gelesen; fehlt die Datei,
greift eine eingebaute Tabelle. Aktuelle Werte (Menge / Stop / Target in Ticks):

| Vorlage | Bracket 1 (T1) | Bracket 2 (Runner) | Max Risk |
|---|---|---|---|
| WADES6 | 2 / 12 / 6 | 1 / 12 / 24 | 5 |
| WADES8 | 2 / 12 / 8 | 1 / 12 / 24 | 7 |
| WADES10 | 2 / 12 / 10 | 1 / 12 / 24 | 9 |
| WADES12 | 2 / 12 / 12 | 1 / 12 / 24 | 11 |
| WADES14 | 2 / 12 / 14 | 1 / 12 / 24 | 13 |
| WADES16 | 2 / 12 / 16 | 1 / 12 / 24 | 15 |
| WADESxxNR | 3 / 12 / xx | – | xx − 1 |

Der Vorlagen-Stop von 12 Ticks gilt nur bis zum ersten Fill; danach ersetzt ihn der Struktur-Stop (5.7).

## 7. Trade-Log (`journal/trade_log.csv`)

- Eine Zeile je Position von flat bis flat (aus dem `PositionClosed`-Event von Nautilus). Die Datei wird bei
  jedem Start neu angelegt.
- **Setup-Name** kommt von der öffnenden Order: GUI-Tag, Sniper-Label + Vorlage, sonst „Manual“.
- **Detailspalten**: Signal-Bar, Trigger, Limit, Initial-SL, Risiko, Ziele, Exits je Rolle (T1, Runner, SL),
  manuelle Verschiebungen, Abstand zur EMA 21.
- **Exit-Grund**: Ein Flatten zählt als Manual; einen automatischen Session-Schluss gibt es nicht.
- Eine Datei mit altem Kurz-Header bleibt stehen; das Log geht dann nach `trade_log_v2.csv`.

## 8. EntryCount (Port `EntryCount.cs`, Regeln EC-1 … EC-11)

Verarbeitung je geschlossener Bar, keine Vorausschau. „Bruch“ = mindestens 1 Tick darüber/darunter.

- **EC-1**: Long- und Short-Zählung laufen getrennt.
  - Long: Ursprung = höchstes Hoch, ein Abwärtsbein bildet sich; der Bruch des Hochs der Signal-Bar = 1EL, 2EL, …
  - Short gespiegelt.
- **EC-2 Inside-Bar**: High ≤ Vor-High und Low ≥ Vor-Low (± halber Tick).
  - Andere Bars werden an der letzten Nicht-Inside-Bar gemessen (Up / Down / NoBreak).
  - Outside-Bar: Standard BothSides (beide Seiten zählen).
- **EC-3 Bein**: Eine Bar in Bein-Richtung startet ein Bein und ist Signal-Bar-Kandidat.
  - Jede weitere Bar im Bein wird neuer Kandidat.
  - Inside-Bars können Kandidat sein, aber nie Trigger.
- **EC-4 Entry**: Eine nicht-inside Bar bricht den Kandidaten; der Zähler steigt um 1, das Label steht an der
  Signal-Bar.
  - Option `RequireEqualOrHigherLeg` (CT: aus): ab dem 2. Bein muss das Bein das vorige Extrem erreichen, sonst
    wird der Entry übersprungen.
- **EC-5 Signal-Bar-Qualität** (nur Anzeige, graues Label):
  - Long: Close > Open, Body ≥ 45 %, Close-Position ≥ 70 %, Ausbruchsdocht ≤ 25 %.
  - Ausnahme Rejection-Bar: Close ≥ 90 % und Docht ≤ 10 %.
- **EC-6 Info**:
  - Trigger = Signal-Bar-Hoch + 1 Tick; Stop = jenseits von Signal-Bar und Vorbar ± 1 Tick.
  - Max Risk 15; Direct ≤ 15 + 2; PullbackLimit wenn fehlende Ticks ≤ 49 % der Struktur; sonst TooLarge.
- **EC-7 F2E**: Nach einem 2E bricht eine spätere nicht-inside Bar die direkte Vorbar um 1 Tick in Gegenrichtung.
  - Ein 3E beendet die Beobachtung.
  - Eine Outside-Bar kann Entry und F2E zugleich sein, wenn die Entry-Seite zuerst gehandelt wurde (Reihenfolge
    aus den Ticks).
- **EC-8 Resets**:
  - a/c Key Level: neuer Kontakt mit der EMA ± 2 Ticks, nur für die Zählung gegen den EMA-Trend; vor der Zählung
    der Bar.
  - b zwei Beine: nach F2E Zähler 0, Ursprung = Extrem seit dem 2E-Trigger.
  - d neues Extrem: Bruch über den Ursprung → Zähler 0, Ursprung = diese Bar.
- **EC-9 Trend**: EMA 21 jetzt minus vor 5 Bars ≥ 2 Ticks → +1, ≤ −2 → −1, sonst 0.
  - Swings: 2 Bars je Seite.
- **EC-11 Pause** (CT: an; C#-Default aus):
  - F2ES bei Trend +1 pausiert die Short-Zählung bis zum ersten Close unter der EMA.
  - F2EL bei Trend −1 pausiert die Long-Zählung bis zum ersten Close über der EMA.
  - Neustart bei 0 mit Ursprung = diese Bar.

## 9. DoubleTrap (Port `DoubleTrap.cs`, Regeln DT-1 … DT-7, DT-a/b)

Long beschrieben; DoubleTrap hostet ein eigenes EntryCount (ohne EC-11).

- **DT-1 Ablauf**: 2EL → F2EL → Signal-Bar suchen (die F2E-Bar darf es sein) → Signal: Entry = High + 1 Tick.
  - Ausgelöst, wenn ein High den Entry erreicht.
  - Verworfen bei einem neuen Korrektur-Tief unter der Signal-Bar; dann wird wieder gesucht.
- **DT-2 Start**: Der 2EL muss mit dem Trend sein (EMA-Trend +1), sonst „not with trend“.
- **DT-3 Weak 2E** (nur Info, Label „DT?“): Die 2E-Signal-Bar liegt mehr als 4 Ticks über der EMA, oder Kongestion,
  oder Regel 5 nicht erfüllt, oder die Gegenstruktur ist intakt.
- **DT-4 Verfall**: neues Hoch über dem Pullback-Start, oder mehr als 10 Bars nach dem F2E ohne Signal.
- **DT-5**: Korrektur-Tief = tiefstes Tief seit dem 2E-Trigger.
- **DT-6 Signal-Bar-Prüfung** (in dieser Reihenfolge):
  1. EMA fällt → nein.
  2. Range: EMA flach und ≥ 4 EMA-Kreuzungen in 30 Bars → nein.
  3. Exhaustion → nein.
  4. Korrektur-Tief unter dem 1E- und 2E-Signal-Bar-Tief.
  5. Bar im Extrem-Bein.
  6. Keine Inside-Bar.
  7. Tief höchstens 2 Ticks über der EMA.
  8. Close über der EMA (sonst ThirdEntryInvalid).
  9. Regel 5 erfüllt.
- **DT-7 Signal**: Stop = Struktur-Stop (Signal-Bar und Vorbar − 1 Tick), Risiko-Klasse wie EC-6.
- **DT-a** (Option, aus): Die Signal-Bar muss selbst das Korrektur-Tief sein.
- **DT-b** (Option, aus): Das Korrektur-Tief muss die Struktur-Stops von 1E und 2E erreichen.

Wades Zielregeln W1–W5 und der Abgleich stehen in der SPEC.

## 10. Bekannte Grenzen

- Databento-Fills ≠ NT-Live-Ticks: Der Anbieter lässt 0,59 % der Fills weg. Bar-Grenzen weichen deshalb
  um ~8 Ticks je Bar ab; ein exakter NT-Live-Nachbau ist nur mit NTs eigenem Tick-Export möglich.
- Die Fill-Reihenfolge innerhalb eines Ticks und Queue-Position bildet die SimulatedExchange nicht ab
  (Fill bei Trade auf dem Limit).
