# PORT_NOTES – EntryCount / DoubleTrap (NT8 → ChartTrader)

Beobachtungen beim 1:1-Port. **Nichts davon ist im Python-Code „verbessert“** – der Port verhält sich wie der
C#-Code. Zeilennummern: `EntryCount.cs` (1942 Zeilen) und `DoubleTrap.cs` (1375 Zeilen), Stand 09.10.2026.
Entscheidung über Vorschläge: erst in `SPEC_EntryCount_DoubleTrap.md`, dann Code, dann `CHANGELOG_Tuning.md`.

## Paritätsstand (10.10.2026)

**EntryCount: 100 % an allen vier Tagen** (06.10. 808/808, 07.10. 925/925, 08.10. 1102/1102, 09.10. bis 15:55
258/258, jeweils inkl. Bar-Zeilen), mit NTs Tick-Export für die Intrabar-Reihenfolge:

    python pa_run.py --config entrycount_doubletrap_nt_chart.toml --nt-ticks "ES 12-26.Last.txt" ^
        --nt-bars "%USERPROFILE%\Documents\NinjaTrader 8\EntryCount_ES_DEC26_2026100[6-9].csv"
    python parity_check.py --day 20261009 --bars

Vorher, ohne Ticks (nur Bar-Farbe):

Eingabe B = die `Bar`-Zeilen der NT-CSV selbst (genau NTs Bars), Parameter wie auf dem NT-Chart
(`entrycount_doubletrap_nt_chart.toml`). Ergebnis EntryCount, 06.–09.10.2026 inkl. Bar-Zeilen:

| Tag | Übereinstimmung | Abweichungen |
|---|---|---|
| 06.10. | 802 / 808 (99,3 %) | 6 × nur Reason „(ticks)“ vs. „(bar color)“ |
| 07.10. | 917 / 925 (99,1 %) | 8 × dito |
| 08.10. | 1095 / 1102 (99,4 %) | 7 × dito |
| 09.10. (bis 15:55) | 251 / 259 (96,9 %) | 7 × dito, davon 1 Entscheidung anders (Bar 36: F2ES gleiche Bar) |

Alle Abweichungen kommen aus der Intrabar-Reihenfolge: NT hat die 1-Tick-Serie, der Lauf ohne Ticks nimmt die
Bar-Farbe. Mit dem NT-Tick-Export (`--nt-ticks`) 100 % (siehe oben). Zähl-, Reset-, F2E-,
Qualitäts- und Risikologik sowie alle Zahlenformate sind auf allen vier Tagen identisch.
DoubleTrap: noch keine NT-CSV vorhanden (ExportCsv auf dem Chart war aus).

## Beobachtungen

| # | Datei, Zeile | Beobachtung | Vorschlag |
|---|---|---|---|
| 1 | EntryCount.cs 704 | Default `RequireEqualOrHigherLeg = false`, die Chart-Instanz vom 06.–09.10. lief aber mit **true** (gespeicherte Property, CSV-Zeile „skipped: … (RequireEqualOrHigherLeg)“ am 06.10. Bar 7). Ohne diese Einstellung nur ~37–49 % Parität. | Parameter in die CSV schreiben (z. B. Kommentarzeile oder `Params`-Event in Bar 1), damit jeder Lauf reproduzierbar ist. Entscheiden, welcher Wert gelten soll. |
| 2 | EntryCount.cs 984–1000 | Ticks mit dem Schlusszeitstempel einer Bar zählen erst für die nächste Bar. Ein Bruch, der nur in diesen Ticks liegt, wird für die Bar selbst nicht gesehen (dann „Seite noch nicht gesehen = zuletzt“). | Beibehalten, nur bewusst sein. |
| 3 | EntryCount.cs 1213/1243 | Reason enthält die Quelle „(ticks)“ bzw. „(bar color)“. Dieselbe Entscheidung ergibt je nach Datenlage einen anderen Text. | Für den Vergleich ok; ggf. Quelle in eigene Spalte. |
| 4 | EntryCount.cs 1276 | `High[0] > c.OriginPrice` ohne `halfTick`, alle anderen Preisvergleiche mit `halfTick`. | Einheitlich `+ halfTick` (wirkt nur bei Gleitkomma-Rauschen). |
| 5 | EntryCount.cs 1095–1103 | Ein übersprungener Entry (RequireEqualOrHigherLeg) wird als Event `Entry` mit Count = n+1 geloggt, obwohl der Zähler nicht steigt. | Eigenes Event `EntrySkipped`, damit Auswertungen „Entry“ nicht falsch zählen. |
| 6 | DoubleTrap.cs 763 | `ExtendLines`: `startAgo = CurrentBar - SigBar + 1` – Entry/Stop-Linie beginnt eine Bar **vor** der Signal-Bar. | Vermutlich gewollt: `CurrentBar - SigBar` (Start an der Signal-Bar). |
| 7 | DoubleTrap.cs 400–416 | Pending-Signal: Erreicht eine Bar den Entry **und** ein neues Korrektur-Extrem, gewinnt der Trigger (Reihenfolge innerhalb der Bar unbekannt). | Wie beim F2E die Intrabar-Reihenfolge (Ticks/Farbe) nutzen. |
| 8 | DoubleTrap.cs 1174/1177 | `ShowEntryCountLabels` und `ShowFirstEntries` haben beide `Order = 1` (nur Anzeige im Property-Grid). | Order 2 vergeben. |
| 9 | DoubleTrap.cs 339 / EntryCount.cs 1024 | `BarStartTime` = `Time[1]` = Ende der Vorkerze (wie gefordert); bei Bar 0 = eigene Zeit. | Keiner. |
| 10 | EntryCount.cs 848–852 | `tradingDay` wird nur mit `ExportCsv` gesetzt (SessionIterator). Für die Logik egal. | Keiner. |
| 11 | EntryCount.cs 1184 / DoubleTrap.cs 752 | Alerts nur in `State.Realtime` – im Port nicht nachgebildet (ein Replay erreicht Realtime nie). | Keiner. |
| 12 | NT-Daten | Der Bar-Export (`TickBarsExporter`) weicht von den Bars der Indikator-CSV ab: einzelne Zeiten 1 s später, am 06.10. andere Open/Close (z. B. Bar 111, 117, 118), NT hat 447 Bars, der Export 446. | Für die Parität die `Bar`-Zeilen der NT-CSV als Eingabe B nehmen (`pa_run.py --nt-bars EntryCount_*.csv`). |

## Annahmen des Ports (Laufzeitumgebung, nicht Logik)

- **Zahlenformat**: .NET Framework 4.8 rundet bei benutzerdefinierten Formaten („0.00##“) erst auf 15 signifikante
  Stellen, dann kaufmännisch (weg von null), und schreibt nie „-0“. `{0}` mit double = „G“ mit 15 Stellen. Über 4 Tage
  CSV ohne einen Formatunterschied bestätigt.
- **Math.Round** (RiskTicks) = Banker's Rounding, wie Python `round`.
- **EMA** wie `@EMA.cs`: erster Wert = erster Close, dann `Close·k + (1−k)·EMA[1]`; DoubleTrap rechnet eine eigene EMA
  (gleiche Werte).
- **Tick-Reihenfolge** (Eingabe A): Ticks mit Zeitstempel < Schlusszeit der Bar (auf `TickTimeResolutionMs`) vor der Bar,
  der Rest danach. Eingabe B mit `--nt-ticks`: auf Sekunden, weil die Bar-Zeiten nur Sekunden haben.
- **Handelstag** für die Dateinamen: CME-Session ab 17:00 Chicago zählt zum nächsten Tag
  (`SessionIterator.ActualTradingDayExchange`). **Bar-Nummern** wechseln am Kalenderdatum in NTs Zeitzone (Berlin).
