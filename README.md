# Charttrader – manual tick replay on NautilusTrader

Range-bar chart that replays Databento trades tick by tick. Orders are placed by click
or hotkey; fills, OCO brackets, positions and PnL come from NautilusTrader's
SimulatedExchange (streaming backtest), not from custom logic.

## Files
| File | Purpose |
|---|---|
| `replay_engine.py` | Wrapper around `BacktestEngine(streaming=True)`; `ManualStrategy` accepts click orders and acts as the broker for the Sniper |
| `range_bars.py` | Range-bar aggregator (NinjaTrader style, fixed tick range, no phantom gap) and tick-bar aggregator |
| `sniper.py` | Port of the NinjaTrader strategy LotsenhofSniper (Smart/Deep/Momentum entries, structure stop, auto-BE, swing trail; without the original's daily loss lockout) |
| `atm_templates.py` | Reads NinjaTrader ATM templates (`Documents/NinjaTrader 8/templates/AtmStrategy/*.xml`) |
| `data_loader.py` | Databento DBN → `TradeTick`; `synthetic()` for tests without data |
| `fetch_databento.py` | Query cost, download trades + definition |
| `export_nt8.py` | Databento ticks → NinjaTrader 8 import file |
| `export_bars.py` | Bars → CSV files like NT's TickBarsExporter indicator |
| `trade_journal.py` | Trade log: one CSV row per closed trade |
| `docs/log_guide.md` | How to read the log messages and the trade log (German, context for trade analysis) |
| `chart_app.py` | PySide6/pyqtgraph UI |
| `chart_cli.py` | Command line of `chart_app.py` and its shell tab completion (argcomplete) |

## Installation
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

If Nautilus is available as a local wheel (the Python version must match the `cpXY` tag),
run `pip install nautilus_trader-*.whl` first, then install the rest from `requirements.txt`.

## Smoke tests (headless, no Qt)
```bash
python smoke_test.py      # Engine: market, limit, bracket/OCO, flatten, jump
python test_sniper.py     # Sniper: setups, trap, fills, stops, BE, trail, partial fill, cancel, scratch
```
Both play constructed ticks through the SimulatedExchange. Run them before every change to
`replay_engine.py` or `sniper.py`.

## Fetching data (September 2025, front contract)
```bash
export DATABENTO_API_KEY=db-...
python fetch_databento.py 2025-09-01 2025-10-01 ES.c.0
```
The script shows the price first and only downloads after confirmation. Result:
`data/ES.c.0-trades.dbn.zst` and `data/ES.c.0-definition.dbn.zst`.
Note: on 2025-09-19 ES rolls from U5 to Z5; `ES.c.0` follows the front contract.
For a single contract, pass `ESZ5` instead.

## Running
```bash
python chart_app.py                                                # synthetic data
python chart_app.py --day 2025-12-08                               # daily file from the default folder
python chart_app.py --day 2025-12-08 2025-12-09 --symbol ESZ5      # multiple days
python chart_app.py TRADES.dbn.zst                                 # or a file path directly
python chart_app.py DAY1.dbn.zst DAY2.dbn.zst --symbol ESH6        # multiple days, fixed contract
python chart_app.py TRADES.dbn.zst --list                          # list contracts in the file
python chart_app.py TRADES.dbn.zst --bars tick:2000                # tick bars like the NT chart
python chart_app.py TRADES.dbn.zst --bars range:6 --atm WADES10,WADES12 --all-buttons
```
Local data is stored per day under `..\trading\databento_data\glbx-mdp3-YYYYMMDD.trades.dbn.zst`
(`--day` builds the path from it, the folder can be changed with `--data-dir`; without `--schema`
the `.trades` file is used if it exists, else the `.mbo` file)
(parent symbology `ES.FUT`, December 2025 to June 2026). These files contain all
expirations and spreads; without `--symbol` the outright contract with the most ticks is
chosen. On roll days (the week before the third Friday of Mar/Jun/Sep/Dec) check `--list`
and specify the contract explicitly. Without `--definition`, an ES contract with tick size 0.25
and multiplier 50 is built from the symbol.

Bar type via `--bars range:N` (ticks per bar range), `--bars tick:N` (Databento trade records
per bar, default `tick:2000`) or `--bars vol:N` (contracts per bar).

**Databento records are not NinjaTrader ticks.** Databento's trades schema contains
one record per aggressor and price level (~3.5 contracts on average). NinjaTrader counts
every individual execution (fill) as a tick; according to the NT export its 2000-tick bars have
~2250 contracts of volume (~1.1 contracts per print). That is why `tick:2000` yields only about
a third of the NT bars. An exact reproduction needs fill granularity, i.e. Databento's MBO schema
(action `F`, one record per filled resting order; per the DBN docs `T` is "an aggressing order
traded", `F` "an existing order was filled"). `vol:N` is only an approximation.

**MBO files are supported directly**: `python chart_app.py ES-mbo.dbn.zst --bars tick:2000`
detects the schema and uses the fills as ticks (`--list` then counts fills per contract).
By date: `--day 2025-09-22 --schema mbo --data-dir D:\trading\GLBX-20260912-SEP25`.
An ES.FUT MBO day (~8M records, ~670k fills for the front contract) loads in ~3 s.

**Export for NinjaTrader 8** (Tools > Historical Data > Import, type Tick):
```bash
python export_nt8.py --day 2025-09-22 2025-09-23 --schema mbo --data-dir D:\trading\GLBX-20260912-SEP25 --out nt8
python export_nt8.py --all --schema mbo --data-dir D:\trading\GLBX-20260912-SEP25 --out nt8 --symbol ESZ5   # whole folder
```
`--all` processes all daily files in the folder one after another (one day in memory) and appends
them per contract to one file. Without `--symbol` each day picks the contract with the most ticks;
across a roll this produces two files (`ES 09-25` and `ES 12-25`).
It writes `nt8/ES 12-25.Last.txt` with `yyyyMMdd HHmmss fffffff;Price;Volume` per fill, timestamps
in UTC (select "UTC" in the import dialog, or use `--tz Europe/Berlin`). The instrument `ES 12-25`
must exist in NT (NT calls it `ES DEC25` in the log). Import the .txt uncompressed: NT's
text importer reads archives as text and then reports "Import field separator could not be
identified". NT's Playback in "Historical" mode then runs on the same ticks.
**Bar export like NT's TickBarsExporter** (same file names and format, for line-by-line comparison):
```bash
python export_bars.py --day 2025-01-02 --data-dir D:\trading\GLBX-20260926-JAN_MAI --symbol ESH5
python export_bars.py --all --data-dir PATH --symbol ESH5 --bars range:4 --out "G:\Meine Ablage\Trading\Bar_Export.csv"
```
Writes `Bar_Export_ESMAR25_2000 - Tick_2025-01-02.csv` (default folder `bar_export/`): bar time = last
tick in Europe/Berlin, decimal comma, UTF-8 with BOM, CRLF, one file per date. Bars restart at the CME
session start; the partial last bar of a session is exported, a bar still forming at the end of the data
is not. Existing files are only extended with newer bars (like ProtectExistingData), `--overwrite`
rewrites them. The previous file(s) are loaded automatically for the session start. Verified against
NT's own exports (ESH5 MBO, Jan/Mar 2025): byte-identical where NT exported complete sessions.
Careful with `--out` into NT's export folder: the file names are the same as NT's, so the files would be extended.

Check the cost beforehand: `python fetch_databento.py 2025-09-22 2025-09-23 ESZ5 --schema mbo --cost-only`
(requires `DATABENTO_API_KEY`). MBO days are considerably larger than trades days.
Candle body width via `--bar-width 50` (percent of bar spacing) or live in the "Width" field.
EMA line via `--ema 21` (0 = off), orange, on bar closes including the running bar.
With `--day`, the previous trading day is loaded automatically as context (Sunday and holiday
files, which only hold the evening Globex reopen, are included and searched past): its candles are in the chart from the start, and "Jump to" shows all
candles from the previous day up to the jump target. For file paths, pass `--context FILE`.
Context ticks only go through the aggregator, not through the engine.
Playback constants (speed factors, idle-gap shortening, drawing window) are at the top of `chart_app.py`.

**Trade log**: every closed trade is appended to `journal/trade_log.csv` (`--journal PATH`, `--journal off`;
not written for synthetic data). One row per position from flat to flat, so an ATM trade with Target1
and runner is one row with the average exit. Columns: `Replayed` (wall-clock time of the replay run),
`Date`, `Open`, `Close` (Europe/Berlin), `Instrument`, `Setup` (`Manual Market`, `Manual Bracket` or the
Sniper setup with ATM template, e.g. `Smart Short WADES16`), `Side`, `Qty`, `Entry`, `Exit`, `Points`,
`Ticks`, `PnL` ($), `Duration`. Excel format for a German system: `;` separator, decimal comma, UTF-8 with BOM.
The log below the chart shows a `JOURNAL` line for each trade.

### Tab completion (zsh)
`chart_app.py` completes its arguments via `argcomplete` (in `requirements.txt`): `--day` offers
the days that have a file in `--data-dir` (only those of `--schema`, if given), `--schema` the
schemas present for the chosen days, `--symbol` the front and next ES contract of those days
(derived from the date, roll 8 days before the third Friday), `--atm` the template names (also after
a comma), `--bars` common bar types, file arguments `*.dbn.zst`. Put `--data-dir` before `--day`
so the days come from that folder. Activation in `~/.zshrc`, with the venv's tools:
```zsh
# ./chart_app.py <Tab>  (script is executable, shebang python3 = active venv)
eval "$(~/ChartTrader/.venv/bin/register-python-argcomplete --shell zsh chart_app.py)"
```
For `python chart_app.py <Tab>` use the global hook once instead (`activate-global-python-argcomplete
--user`, then follow its note on `fpath`); it completes every script with the `PYTHON_ARGCOMPLETE_OK`
marker. In both cases the `python3` in `PATH` must be the venv's (activate the venv first).

## Controls
| Key / mouse | Action |
|---|---|
| Space | Play / pause – real-time: at 1x data time passes like the clock; gaps without trades are shortened to 3 s |
| → / ← | Speed factor up / down, 0.5x … 100x (relative to data time); shown in the status line |
| N | Advance one range bar |
| Mouse wheel | More / fewer bars in view (20 to 600) |
| Ctrl+mouse wheel, Ctrl+vertical drag | Compress / stretch price axis; view follows price |
| R | Price axis back to auto |
| Hover over a bar | Info box next to the pointer: bar number, date, time from–to (time zone of "Jump to"), open, high, low, close, volume, size in ticks, duration. Works for the running bar too and updates during playback |
| H | Horizontal line at the mouse pointer with the price (snapped to the tick) on the right, on/off; also button "Crosshair (H)", default on. It ignores the mouse, so stop/target lines and drawings underneath can still be dragged |
| T | Trend line: two left clicks place the line (preview follows the mouse), then drawing mode turns off; right click or Esc cancels. The line is extended to the right as a dashed line |
| A | Text: click the position in the chart, enter the text in the dialog (Enter = OK, Shift+Enter = new line); then the mode is off again. Right click or Esc cancels |
| Double click on text | Edit the text (emptying it deletes it) |
| Click on line / text | Select it (yellow; a line shows its endpoints as handles); clicking empty space clears the selection |
| Drag | Move line or text; on a line endpoint, only that point; Shift+drag drags a copy |
| Ctrl+C | Copy selected line or text (the copy appears slightly offset and is selected); a text is also put on the clipboard |
| Del / Shift+Del | Delete the selected drawing (without selection: the last one) / all; a jump deletes all because bar indices are rebuilt |
| Ctrl+B / Ctrl+S | Market buy / sell |
| Shift+B / Shift+S | Bracket (market + OCO target/stop, ticks from the input fields) |
| W / S | Sniper: Smart Long / Short (arm trap) |
| Ctrl+↑ / Ctrl+↓ | Sniper: Momentum Long / Short |
| X | Sniper: Cancel All (trap, entries, positions) |
| C | Sniper: Scratch, targets to breakeven + offset |
| Mouse click in chart | Places no orders; trading only via buttons and keys (clicks are only for trend lines) |
| Drag stop/target line | Moves the stop or target of a filled bracket (manual bracket and Sniper; drawn thick). Takes effect on the next tick, the line shows the new price in the meantime. A stop on the wrong side of the market is rejected by the exchange and snaps back. The Sniper keeps manual moves and only tightens from there (BE, trail) |
| F | Flatten: cancel orders, close position |
| Jump to + Go | Date/time in the selected time zone (default Europe/Berlin); shows the last ~20k ticks as context bars, replay starts there. Only when flat with no open orders, forward only |

Blue = limit, purple = stop, yellow dashed = last price, triangles = fills,
orange dashed = trap trigger, thick blue = trap limit.

## Sniper (port of LotsenhofSniper)
The panel on the right corresponds to the WPF buttons of the NinjaTrader strategy. ATM templates
are read directly from `Documents\NinjaTrader 8\templates\AtmStrategy\<Name>.xml`
(quantity, stop, target per bracket, NT's own auto-breakeven); max risk = number in the name − 1.

- **Smart Long/Short (W/S)**: signal bar = last closed bar. Filters: close position ≥ 70 %,
  body ≥ 5 %, breakout wick ≤ 25 %, no inside bar. Stop beyond the two-bar extreme,
  trigger 1 tick above/below the signal bar. If the risk fits (max risk + 2 ticks tolerance),
  it buys at the trigger; otherwise a limit is placed at stop + max risk, provided the required
  pullback is ≤ 49 % of the structure. The trap is valid only for the current bar.
- **No Runner**: same logic with template `<ATM>NR` (one bracket, no runner).
- **Deep / Momentum** (`--all-buttons`): limit 49 % into the signal bar, or an immediate stop-limit.
- **After the fill**: Stop1 moves to the structural stop, the runner stop as well once the entry
  is complete. ATM breakeven from the template (e.g. from +10 ticks). After the Target1 fill the runner
  stop moves to breakeven, then swing trail (strength 2, min. 8 ticks prominence, 1 tick buffer).
- **Cancellation**: unfilled entry after 10 ticks of runway or 3 bars; partial-fill remainder after 10 ticks.
- **No daily lockout**: unlike the NT strategy, a losing trade does not lock further entries in the
  replay, so a day can be traded through for practice.

Parameters: `SniperConfig` in `sniper.py` (defaults as in the NT strategy).

## Known limitations (first version)
- PnL is in points × the instrument's multiplier. Without a definition file a
  test-kit ES is used; always load the definition for real $ values.
- Fills are "trade-through": limits fill as soon as a trade reaches the price,
  without queue position. For more conservative assumptions, set a `FillModel` in `ReplayEngine`.
- No session filters (RTH/ETH). Next steps: session filter, multiple range sizes.
