Portiere die bestehenden NinjaTrader-8-Indikatoren EntryCount.cs und DoubleTrap.cs 1:1 in den ChartTrader (Python, NautilusTrader), damit ich dort im Playback mit Databento-Daten feinjustieren kann. Danach wird jede Änderung kontrolliert nach NT8 zurückübertragen.

GRUNDSATZ
- Portierung, keine Verbesserung. Der Python-Code muss sich exakt so verhalten wie der vorhandene C#-Code, inklusive aller // ASSUMPTION:-Stellen, Sonderfälle und Defaults. Wenn dir im C#-Code etwas falsch oder unklar vorkommt: NICHT ändern, sondern in PORT_NOTES.md notieren (Datei, Zeile, Beobachtung, Vorschlag).
- Eine Klasse/Funktion im Python-Code pro Methode im C#-Code, gleiche Reihenfolge, gleiche Namen (snake_case erlaubt). Neben jede Funktion einen Kommentar mit der C#-Quelle (z. B. "# port of EntryCount.OnBarUpdate, lines 210-265").
- Gleiche Parameternamen und Defaults wie die NinjaScript-Properties, zentral in einer Konfigurationsdatei (z. B. entrycount_doubletrap_config.py oder .toml), damit ich sie ohne Code ändern kann.
- Code, Kommentare und Log-Texte auf Englisch.

SCHRITT 0 – NT8-SEITE DIAGNOSEFÄHIG MACHEN (nur Ausgabe, keine Logikänderung)
Falls EntryCount.cs und DoubleTrap.cs noch kein Ereignis-Export haben, ergänze in beiden NUR:
- Property ExportCsv (Default false) und DebugLog (Default false)
- Pro Ereignis eine Zeile in Documents\NinjaTrader 8\EntryCount_<Instrument>_<yyyyMMdd>.csv bzw. DoubleTrap_…csv, Trennzeichen ";", Dezimalkomma (de-DE), Kopfzeile:
  BarEndTime;BarStartTime;Indicator;Event;Direction;Count;SignalH;SignalL;SignalO;SignalC;EMA;DistEmaTicks;BodyPct;ClosePct;WickPct;QualityOk;Trigger;Stop;RiskTicks;RiskClass;Reason
- BarStartTime = Zeitstempel der Vorkerze. Felder, die die aktuelle Logik nicht berechnet, bleiben leer (nicht neu berechnen).
- Event ∈ {Entry, F2E, Reset, DtCandidate, DtSignal, DtRejected, Weak2E, ThirdEntryInvalid}. Reason = Reset-Grund bzw. der Filter, der ein DT-Signal verworfen hat, soweit die Logik ihn kennt.
- Gepuffert schreiben, in State.Terminated schließen. Zeige mir den Diff dieser Ausgabe-Ergänzung separat, bevor du weitermachst, und bestätige, dass keine Zähl- oder Filterlogik verändert wurde.

SCHRITT 1 – PORTIERUNG IN DEN CHARTTRADER
- Eingabe A: die Bars des ChartTraders (Databento-Playback, 2000 Tick).
- Eingabe B: NT8-Bar-Export (Bar_Export_*.csv, Trennzeichen "|", Spalten DateTime|Open|High|Low|Close|Volume, DateTime = Bar-ENDE). Doppelte Zeilen durch Chart-Reload entfernen: eine Bar ist Duplikat, wenn High, Low, Close und Volume mit der Vorzeile übereinstimmen und ihre Zeit ≤ 2 s später liegt (die erste behalten).
- Beide Eingaben laufen durch dieselbe Logik. Verarbeitung strikt Bar für Bar beim Bar-Schluss wie Calculate.OnBarClose, keine Vorausschau.
- EMA wie NT8: Seed mit dem ersten Close, danach EMA = Close·k + EMA[1]·(1−k), k = 2/(Period+1), über die volle Datei.
- Tick Size 0,25, alle Abstände in Ticks wie im C#-Code.
- Ausgabe: dieselbe Ereignis-CSV wie in Schritt 0 (identisches Format, Dateiname mit Suffix _CT), plus Anzeige im CT-Chart: Labels 1EL/2EL…, F2EL/F2ES, DT, DT?, ThirdEntryInvalid, Entry-/Stop-Linie, so wie NT8 sie zeichnet. Alle Anzeigen per Konfiguration schaltbar.
- Die Testfälle aus dem Kommentarblock des C#-Codes als pytest-Tests mit kleinen, konstruierten Bar-Folgen umsetzen.

SCHRITT 2 – PARITÄTSPRÜFUNG
- Skript parity_check.py: liest die NT8-CSV und die CT-CSV desselben Tages (CT mit Eingabe B, also denselben NT8-Bars) und vergleicht Zeile für Zeile über BarEndTime + Indicator + Event + Direction.
- Ausgabe: Anzahl Übereinstimmungen, nur-NT8, nur-CT, und bei gleichem Schlüssel jede abweichende Spalte (Preise mit Toleranz 0, Prozentwerte mit Toleranz 0,5).
- Ziel: 100 % Übereinstimmung. Bei Abweichungen erst die Ursache finden und den PORT beheben, niemals den C#-Code anpassen, um Parität zu erzwingen.
- Ich liefere dafür den NT8-Bar-Export und die NT8-Ereignis-CSV eines Tages (erster Testtag: 09.10.2026, ES 12-26).

SCHRITT 3 – FEINJUSTIERUNG (erst nach bestandener Parität)
- Führe eine Datei SPEC_EntryCount_DoubleTrap.md (Ausgangsstand: das Verhalten des C#-Codes plus PORT_NOTES) und eine Datei CHANGELOG_Tuning.md.
- Jede Änderung, die ich anfordere, zuerst in der SPEC eintragen, dann im Python-Code umsetzen, dann im CHANGELOG festhalten: Datum, Parameter oder Regel, alt → neu, Begründung, Testtag(e), Wirkung (Anzahl Signale vorher/nachher aus der Ereignis-CSV).
- Keine Änderung ohne CHANGELOG-Eintrag.

SCHRITT 4 – RÜCKÜBERTRAGUNG NACH NT8 (wenn ich es anfordere)
- Übertrage ausschließlich die Einträge aus CHANGELOG_Tuning.md seit der letzten Übertragung auf EntryCount.cs und DoubleTrap.cs. Nichts anderes ändern.
- Danach Parität erneut prüfen: NT8-CSV gegen CT-CSV für alle Testtage aus dem CHANGELOG. Erst bei 100 % Übereinstimmung gilt die Übertragung als abgeschlossen; im CHANGELOG mit Datum als "übertragen" markieren.

LIEFERUNG JETZT
Schritt 0 (Diff zeigen und auf mein OK warten), danach Schritt 1 und das Skript aus Schritt 2. Am Ende: kurze Anleitung, wie ich im CT einen Tag mit Eingabe A bzw. B starte, wo die CSVs landen und wie ich parity_check.py aufrufe.
