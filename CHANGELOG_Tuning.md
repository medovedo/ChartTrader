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
| | | | | | | | |
