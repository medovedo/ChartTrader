# CHANGELOG Tuning – EntryCount / DoubleTrap

Jede Änderung: erst `SPEC_EntryCount_DoubleTrap.md`, dann Code/TOML, dann hier. Keine Änderung ohne Eintrag.
Rückübertragung nach NT8 (Schritt 4) überträgt genau die Einträge mit Status „offen“ und markiert sie dann
„übertragen am …“ – erst nach 100 % Parität NT8-CSV gegen CT-CSV an allen Testtagen des Eintrags.

**Wirkung messen:** Testtage mit NTs Bars und Ticks durchrechnen, vorher/nachher vergleichen:

    python pa_run.py --nt-bars "<NT>\EntryCount_ES_DEC26_2026100[6-9].csv" --nt-ticks "ES 12-26.Last.txt" --out pa_csv\vorher
    (Änderung)
    python pa_run.py ... --out pa_csv\nachher
    python pa_stats.py pa_csv\vorher pa_csv\nachher

**Testtage:** ES DEC26, 2000 Tick, 06.–09.10.2026 (09.10. nur bis 15:55, so weit reicht die NT-CSV).

**Ausgangsstand = C#-Defaults, RequireEqualOrHigherLeg = false** (Entscheidung 10.10.2026; `pa_csv/base_false`).
Zum Vergleich der NT-Chart-Stand mit true (`pa_csv/base_true`). Summe 06.–09.10.:

| | 1EL | 2EL | 2EL Q ok | F2EL | 1ES | 2ES | 2ES Q ok | F2ES | übersprungen |
|---|---|---|---|---|---|---|---|---|---|
| RequireEqualOrHigherLeg false | 265 | 87 | 26 | 60 | 260 | 93 | 20 | 67 | 0 |
| RequireEqualOrHigherLeg true | 261 | 66 | 20 | 52 | 255 | 72 | 12 | 60 | 51 |

---

| Nr. | Datum | Regel / Parameter | alt → neu | Begründung | Testtage | Wirkung | Status NT8 |
|---|---|---|---|---|---|---|---|
| 1 | 10.10.2026 | EC-11 neu: `PauseCountAfterCounterTrendF2E` (EntryCount). F2ES bei Trend +1 pausiert die Short-Zählung bis zum ersten Close unter der EMA; F2EL bei Trend −1 pausiert die Long-Zählung bis zum ersten Close über der EMA; Neustart dort bei 0 (Ursprung = diese Bar). Events `CountPaused`/`CountResumed`, Bar-Zustand „Paused“. | neue Option, Default **false** (aus) | Nutzer: nach dem gescheiterten Gegentrend-2E im Trend nicht weiter gegen den Trend zählen, bis der Preis die EMA-Seite wechselt. | 06.–09.10.2026 (ES DEC26, NT-Bars + NT-Ticks) | eingeschaltet, Summe 4 Tage: 1ES 260→193, **2ES 93→68**, F2ES 67→49; 1EL 265→227, **2EL 87→68**, F2EL 60→46; 27 Short- und 20 Long-Pausen. Ausgeschaltet: Parität zu NT unverändert 100 %. | offen |
| 2 | 10.10.2026 | EC-11: Voreinstellung im CT (`entrycount_doubletrap.toml`) | `PauseCountAfterCounterTrendF2E` false → **true** | Nutzer: aktuelle Einstellung als CT-Default. Code-Default bleibt false (C#-Verhalten, Parität). | 06.–09.10.2026 | wie Nr. 1 (Summe 4 Tage: 2ES 93→68, 2EL 87→68); Ausgangsstand für weitere Vergleiche ist ab jetzt `pa_csv/ec11_pause` | offen |
| 3 | 10.10.2026 | DT-a neu: `DtSignalBarIsCorrectionExtreme` (DoubleTrap). Die Signal-Bar muss selbst das Korrektur-Tief (Long) bzw. -Hoch (Short) seit dem 2E-Trigger sein (gleich tief zählt); ersetzt „Bar im Extrem-Bein“. Reason „bar is not the correction low/high (Preis @bar n)“. | neue Option, Default **false** (aus) | Wade Regel 5: die Signal-Bar ist das absolute Extrem der Korrektur. | 06.–09.10.2026 | eingeschaltet: DT-Signale unverändert 2 (beide erfüllen die Regel); 10 Kandidaten früher abgelehnt (vorher aus anderen Gründen abgelehnt). Ausgeschaltet: DT-CSV byte-gleich. | offen |
| 4 | 10.10.2026 | DT-b neu: `DtRequireEntryStopsHit` (DoubleTrap). Das Korrektur-Extrem muss die Struktur-Stops von 1E (falls Teil der Folge) und 2E erreichen statt nur unter deren Signal-Bar-Tiefs zu liegen. Reason „no new low/high at/beyond the 1E/2E stop (Preis)“. | neue Option, Default **false** (aus) | Wade Regel 5: die DT-Signal-Bar stoppt den ersten und zweiten Trend-Entry aus. | 06.–09.10.2026 | eingeschaltet: DT-Signale unverändert 2; 17 Kandidaten früher abgelehnt. Ausgeschaltet: DT-CSV byte-gleich. | offen |
