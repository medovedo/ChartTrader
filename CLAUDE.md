# Charttrader – Projektkontext für Claude Code

Antworte auf Deutsch. Code komplett auf Englisch: Bezeichner, Kommentare, Docstrings,
GUI-Texte und Meldungen. README ebenfalls auf Englisch.

## Was das ist
Manueller Tick-Replay-Charttrader für ES-Futures auf Basis von NautilusTrader (>= 1.231).
Der Nutzer spielt Databento-Trades (DBN, Schema `trades`) tickweise ab, handelt per Klick/Taste
im Range-Bar-Chart, und **alle Fills, OCO-Brackets, Positionen und PnL kommen aus der
SimulatedExchange von Nautilus** (Streaming-Backtest), nicht aus eigener Logik. Das ist eine
bewusste Architekturentscheidung – keine eigene Fill-Simulation bauen.

## Architektur
- `replay_engine.py` – `ReplayEngine` kapselt `BacktestEngine.run(streaming=True)`;
  Ticks werden in Batches (`step(n)`) gefüttert, `clear_data()` nach jedem Batch.
  `ManualStrategy` hat keine eigene Logik, sie ist nur das Sprachrohr für GUI-Orders
  (`market`, `limit`, `stop_market`, `bracket` via `order_factory.bracket`, `flatten`).
  Achtung: Methode heißt `stop_market`, nicht `stop` (Namenskollision mit `Component.stop`).
  `skip_to(idx)` setzt den Lesezeiger ohne Engine-Verarbeitung – nur flat erlaubt.
- `range_bars.py` – Range-Bars im NinjaTrader-Stil: fester Tickbereich, Bar schließt am Rand,
  nächste Bar öffnet am Close der vorigen (kein Phantom-Gap). Nautilus hat keine RANGE-Aggregation.
  Dazu `TickBarAggregator` (N Trades je Bar, `current` ist nach dem Schluss None) und
  `make_aggregator("range:4" | "tick:2000")`. Der Aggregator lebt in `ManualStrategy.agg`
  und wird in `on_trade_tick` gefüttert; die GUI liest ihn nur (`engine.agg`).
- `sniper.py` – Port von `LotsenhofSniper.cs` (NinjaTrader, Ordner
  `Documents\NinjaTrader 8\bin\Custom\Strategies`). Reine Funktionen (`signal_bar_check`,
  `smart_setup`, `deep_setup`, `momentum_setup`, `swing_trail_candidate`) plus Zustandsmaschine
  `Sniper` (Trap → `AtmTrade` mit je einem Nautilus-Bracket pro ATM-Bracket). Läuft pro Tick in
  `ManualStrategy.on_trade_tick`, spricht Orders über `place_bracket/order_view/modify/cancel/close_all`
  an. Modify/Cancel außerhalb eines Ticks werden erst beim nächsten `engine.run` wirksam.
  Bar-Konvention wie NinjaScript: Bar[1] = `agg.bars[-1]` (Signal-Bar), Bar[0] = `agg.current`.
- `atm_templates.py` – liest NT-ATM-XML (`Documents\NinjaTrader 8\templates\AtmStrategy`),
  Fallback-Tabelle für WADES6..16 (+NR). Max Risk = Zahl im Namen − 1.
- `data_loader.py` – `load_databento(trades | [trades...], definition=None, start, end, symbol=None)`
  über `DatabentoDataLoader`. Parent-Symbologie (`ES.FUT`) wird über die DBN-Metadaten
  aufgelöst (`price_precision=2` nötig, wenn keine Definition geladen ist); dann wird ein
  Outright gewählt: `symbol` oder meiste Ticks. Ohne Definition `es_contract_from_symbol()`
  (Tick 0.25, Multiplikator 50). `contracts_in()` zählt Ticks je Kontrakt (`--list`).
  `inspect_dbn()` prüft Schema/Symbole/Zeitraum. `synthetic()` liefert Testticks (monoton steigende ts!).
  **Databento-Trades ≠ NinjaTrader-Ticks**: Trades-Schema = ein Record je Aggressor (Action T,
  Ø ~3,5 Kontrakte); NT zählt Fills. MBO-Dateien (Action F = Fill) werden erkannt und via
  `mbo_fill_ticks()` / `fills_to_ticks()` (NumPy-Filter) zu Ticks. Details unten unter
  "Tick-Daten: gemessene Fakten". `vol:N`-Bars existieren, wurden vom Nutzer als Ersatz abgelehnt.
- `chart_app.py` – PySide6 + pyqtgraph. `CandleItem` zeichnet Bars mit x = Bar-Index
  (nur die letzten `VISIBLE_BARS`, Offset `start`). Bedienung siehe README.
  Wiedergabe ist zeitgetreu: simulierte Uhr `sim_ns` in Datenzeit, Timer alle `FRAME_MS`,
  pro Aufruf werden alle Ticks mit `ts_event <= sim_ns` gespielt (bisect auf `ts_index`).
  Faktor aus `SPEEDS`, Pausen > `MAX_IDLE_NS` werden gekürzt. Nach N, Sprung und Pause
  `_sync_clock()` aufrufen, sonst holt die Uhr nach. "Springe zu" = bisect auf `engine.ts_index`,
  Aggregator zurücksetzen, `context_ticks` (Vortag, via `--day` automatisch) + alle Ticks bis zum
  Ziel nur durch den Aggregator, dann `engine.skip_to`. EMA (`--ema`, Cache `_ema` je Aggregator)
  und Körperbreite (`--bar-width`, Feld "Breite") sind Anzeigeoptionen.
- `fetch_databento.py` – Kosten abfragen, dann Trades + Definition laden.
- `export_bars.py` – Bars als CSV wie NTs Indikator `TickBarsExporter.cs` (Dateiname
  `Bar_Export_ESMAR25_2000 - Tick_<Datum>.csv`, Zeit = letzter Tick in Berlin, Dezimalkomma, BOM, CRLF,
  nur anhängen). Streamt Vortagsdateien + Tage, Session-Ende ohne Folge-Tick via `agg.end_session()`.
  Gegen NT-Exporte ESH5 (MBO) verifiziert: byte-identisch (2.1., 3.1., 9.1., 10.1., 5.–6.3.2025);
  NT-Dateien mit Playback-Sprung/Abbruch sind Präfix bzw. Teilmenge. NT lässt die letzte Chart-Bar weg
  (`Bars.Count - 2`), daher fehlt dort am Datenende die Session-Teilbar.

## Verifiziert (headless, QT_QPA_PLATFORM=offscreen)
Market-Entry, Limit-Target-Fill, OCO löscht Gegenorder, Flatten, Sprung + Trade danach,
Rücksprung wird abgelehnt, Bracket-Stop verschieben (`move_order`, wirksam beim nächsten Tick;
Stop auf falscher Marktseite → MODIFY REJECTED) (`smoke_test.py`). Ziehen in der GUI: Nur offene
Orders mit Parent (= Bracket-Legs nach Entry-Fill) sind ziehbar; `pending_moves` zeigt den neuen Preis bis
zur Bestätigung. `Sniper.on_manual_move` übernimmt manuelle Stops (Trail/BE ziehen nur enger nach). Zeitgetreue Wiedergabe: 1x/10x folgen der
Uhr, Pause/N/Sprung synchronisieren, 60-s-Lücke wird zu ~3 s. Databento-Laden aus lokalen
DBN-Dateien (Ein- und Mehrtages, Kontraktauswahl) verifiziert; `fetch_databento.py` (API) ungetestet.
Sniper (`test_sniper.py`, Tick-Bars mit konstruierten Pfaden): Smart Long Fill → Stop1/Stop2 auf
Struktur-Stop → Target1 (WADES-Vorlagen haben kein ATM-BE mehr) → Auto-BE → Swing-Trail → Runner-Exit (PnL 375); Short-Verlust →
Sperre; Runway- und Bars-to-Wait-Storno; Scratch; Cancel All; No-Runner; Momentum-Stop-Limit.
Teilfill-Storno ist ungetestet (Nautilus füllt je Trade-Volumen, Teilfills sind schwer zu konstruieren).
Nautilus-Fill-Regel (empirisch): Limit füllt zum Limitpreis, sobald ein Trade dort handelt, mit dem
Trade-Volumen; Bid/Ask kommen aus den Aggressor-Seiten – synthetische Ticks brauchen daher
Aggressor = Preisrichtung, sonst füllen Limits zu falschen Preisen.

## Nutzer-Kontext
Der Nutzer handelt diskretionär ES/NQ (PATs-Stil, Range-Bars), Handelsfenster 15:00–17:30
Europe/Berlin (US Cash Open), Regel: nach einem Verlust-Trade ist der Tag beendet.
Lokale Daten: `C:\Users\Marko\Desktop\trading\databento_data\glbx-mdp3-YYYYMMDD.trades.dbn.zst`,
ein UTC-Tag je Datei, Dezember 2025 bis Juni 2026, Parent-Symbologie `ES.FUT` (alle Laufzeiten
und Spreads gemischt, ~250k–600k Ticks je Kontrakt und Tag, ts_event == ts_init, monoton).
MBO-Daten (Fill-Granularität, ES.FUT Parent): `D:\trading\GLBX-20260912-SEP25\glbx-mdp3-YYYYMMDD.mbo.dbn.zst`,
7.–30. September 2025, ~8 Mio. Datensätze/Tag, ~670k Fills für ESZ5 (Ø 1,5 Kontrakte je Fill).
`export_nt8.py` schreibt daraus NT8-Import-Dateien (`ES 12-25.Last.txt`).
MBO August 2026 (12.–14.8., ESU6): `D:\trading\GLBX-20260912-Q3LAP6TSC5\`. NT-Exporte (Tick-Bars
per `TickBarsExporter`, Ticks per Historical Data > Export) liegen unter `G:\Meine Ablage\Trading\`.
Er kann Python und C#, schreibt Rust. Er handelt in NinjaTrader mit der eigenen Strategie
LotsenhofSniper (C#, Git-Repo im Strategies-Ordner) und ATM-Vorlagen WADES6..16 (+NR); Charts dort
sind 2000-Tick-Bars mit EMA 21. NT-Verbindungen laut Config.xml: "My NinjaTrader Continuum" (CQG)
und Kinetick EOD; die Live-Verbindung heißt im Log "Live" (Anbieter nicht aus der Config ablesbar).

## Tick-Daten: gemessene Fakten (Sept. 2026, nicht erneut erarbeiten)
Quelle: Vergleich Databento MBO (ESU6, Session 13.8.2026) gegen NTs eigenen Tick-Export
(`ES 09-26.Last.txt`, Format `yyyyMMdd HHmmss fffffff;Last;Bid;Ask;Vol`, UTC, ms-Auflösung).
- **Ein NT-Tick = ein Databento-Fill (Action F).** NTs Prints sind eine Teilmenge der Fills:
  700.150 Prints vs. 704.337 Fills, nur 57 Prints ohne Fill (Eröffnungsauktion, dort fasst der
  Anbieter das Matching zu einem Print zusammen). Zeitversatz Print↔Fill: Median 0,1 ms.
- **Der Anbieter lässt 0,59 % der Fills weg** (4.147, mit 1,0 % des Volumens), gleichmäßig über den
  Tag, proportional zur Aktivität. Rund 1.500 davon sind "Aggressor-Fills": bei ~4.100 Preisstufen
  meldet MBO zusätzlich zur ruhenden Seite einen F-Record der Aggressor-Order (Fill-Summe > Größe des
  T-Records). Der Anbieter lässt diese aber nicht konsequent weg (2.600 davon hat er), und ~2.600
  fehlende sind normale Ein-Lot-Fills gegen ruhende Orders. **Keine deterministische Regel gefunden**
  (geprüft: Seite, Flags, Sweep-Position/-Länge, Latenz, Sequenz, Duplikate, Zeitstempel-Kappung).
  Exakte 1:1-Nachbildung des NT-Live-Charts aus Databento ist damit nicht möglich; Bar-Grenzen
  wandern um ~8 Ticks je Bar, ~1 Bar je 250.
- **Bar-Dichte stimmt trotzdem**: je Stunde 53/66/62/29 NT-Bars vs. 53,8/65,8/62,8/29,3 aus Fills.
  Trades-Schema (`tick:2000` auf Trades-Dateien) ergibt nur ~1/3 der NT-Bars – nie dafür verwenden.
- **Import in NT8 ist verlustfrei**: importierte Fills (`export_nt8.py`) zählen in NT 1:1 (Asien-
  Session 22.9.2025: Drift ±11 Ticks bei 20.000). NT-Import liest kein ZIP (StreamReader);
  Zeitzone im Import-Dialog = Zeitzone der Datei (Standard UTC). Beim Import den Vortag (UTC-Datei)
  mitgeben, sonst beginnt NTs Zählung mit Anbieter-Vorabdaten ab 00:00 Berlin und die Phase
  verschiebt sich. NT-Session endet in den Exporten um 19:59:59 UTC.
- **Empfehlung**: Live-Feed behalten (99,4 % Fill-Abdeckung ist so fein wie es in NT geht);
  Historie/Replay aus Databento MBO in beiden Systemen (NT Playback "Historical" auf importierten
  Fills ist deckungsgleich mit dem Charttrader). Anbieterunabhängig sind nur preisbasierte Bars
  (Range). `tick:2012` träfe NTs Bar-Zahl statistisch, nicht die Grenzen.
- Der Google-Drive-Export `Bar_Export_*.csv` wird von NT während des Playbacks laufend neu
  geschrieben und ist erst nach Ende des Playbacks stabil.

## Nächste Schritte (in dieser Reihenfolge sinnvoll)
1. Trade-Journal als CSV (Zeit, Seite, Menge, Entry, Exit, PnL, Dauer) aus Nautilus-Positions.
2. RTH/ETH-Sessionfilter und optional "Handelsfenster 15:00–17:30" als Sichtbarkeitsmarkierung.
3. Range-Größe zur Laufzeit umschaltbar (Aggregator neu aufbauen aus bereits gespielten Ticks).
4. `FillModel` konfigurierbar machen (Queue-Position / Slippage) statt reinem Trade-Through.
5. Performance: Millionen Ticks → Ladezeit/RAM; Tages- oder Sessionweise laden.

## Konventionen
- Keine Browser-Storage-APIs, keine externen Chart-Libs; pyqtgraph bleibt.
- Vor Änderungen an der Engine den Headless-Smoke-Test laufen lassen (siehe README).
- `pip install --break-system-packages` nur im Container; lokal venv.
