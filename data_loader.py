"""Loads trade ticks from Databento DBN files (or synthetic ones for tests)."""
from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity

MONTH_CODES = "FGHJKMNQUVXZ"


def es_contract(expiry_year: int = 2025, expiry_month: int = 12) -> Instrument:
    """Real ES contract (tick 0.25, multiplier 50) for use without a definition file."""
    from datetime import datetime, timezone
    from nautilus_trader.model.enums import AssetClass
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import FuturesContract
    from nautilus_trader.core.datetime import dt_to_unix_nanos
    from nautilus_trader.model.currencies import USD
    sym = f"ES{MONTH_CODES[expiry_month - 1]}{expiry_year % 10}"
    exp = dt_to_unix_nanos(datetime(expiry_year, expiry_month, 20, tzinfo=timezone.utc))
    return FuturesContract(
        instrument_id=InstrumentId(Symbol(sym), Venue("GLBX")), raw_symbol=Symbol(sym),
        asset_class=AssetClass.INDEX, currency=USD, price_precision=2,
        price_increment=Price.from_str("0.25"), multiplier=Quantity.from_int(50),
        lot_size=Quantity.from_int(1), underlying="ES", activation_ns=0, expiration_ns=exp,
        ts_event=0, ts_init=0,
    )


def es_contract_from_symbol(symbol: str) -> Instrument:
    """Contract from a symbol like 'ESH6' (month code + year digit, 2020s decade)."""
    sym = symbol.split(".")[0]
    if len(sym) != 4 or not sym.startswith("ES") or sym[2] not in MONTH_CODES or not sym[3].isdigit():
        raise ValueError(f"Not an ES outright symbol: {symbol}")
    return es_contract(2020 + int(sym[3]), MONTH_CODES.index(sym[2]) + 1)


def inspect_dbn(path: str | Path) -> dict:
    """Schema, symbols and time range of a DBN file (without loading everything)."""
    import databento as db
    store = db.DBNStore.from_file(path)
    m = store.metadata
    return {"schema": str(m.schema), "symbols": m.symbols, "stype_in": str(m.stype_in),
            "start": m.start, "end": m.end, "dataset": m.dataset}


def fills_to_ticks(arr, id_to_symbol: dict[int, str], venue: str = "GLBX") -> list[TradeTick]:
    """MBO records (NumPy structure from DBNStore.to_ndarray) -> one TradeTick per fill (action 'F').

    A fill is the execution of a resting order; this matches what NinjaTrader counts
    as a single tick. The fill's `side` is the side of the resting order, so the
    aggressor is the opposite side. Timestamp = ts_recv (monotonic, as in the trades schema).
    """
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    fills = arr[arr["action"] == b"F"]
    ids: dict[int, InstrumentId] = {}
    out: list[TradeTick] = []
    for k, r in enumerate(fills):
        iid = int(r["instrument_id"])
        inst_id = ids.get(iid)
        if inst_id is None:
            sym = id_to_symbol.get(iid)
            if sym is None:
                continue
            inst_id = ids[iid] = InstrumentId(Symbol(sym), Venue(venue))
        side = r["side"]
        aggr = AggressorSide.BUYER if side == b"A" else AggressorSide.SELLER if side == b"B" else AggressorSide.NO_AGGRESSOR
        ts = int(r["ts_recv"])
        out.append(TradeTick(inst_id, Price(int(r["price"]) / 1e9, 2), Quantity.from_int(int(r["size"])),
                             aggr, TradeId(f"{int(r['sequence'])}-{k}"), ts, ts))
    return out


def select_fills(fills, id_to_symbol: dict[int, str], symbol: str | None = None):
    """Restrict the fill array to one outright contract: `symbol` (e.g. 'ESZ5') or the one with most fills.
    Returns (symbol, filtered array, {symbol: count})."""
    import numpy as np
    ids, counts = np.unique(fills["instrument_id"], return_counts=True)
    per_symbol = {id_to_symbol[int(i)]: int(n) for i, n in zip(ids, counts) if int(i) in id_to_symbol}
    outrights = {k: n for k, n in per_symbol.items() if "-" not in k}
    if not outrights:
        raise ValueError("No fills for outright contracts in the MBO file.")
    if symbol:
        chosen = symbol.split(".")[0]
        if chosen not in per_symbol:
            raise ValueError(f"Contract {chosen} not in the data. Available: "
                             + ", ".join(f"{k} ({n})" for k, n in sorted(outrights.items(), key=lambda kv: -kv[1])))
    else:
        chosen = max(outrights, key=outrights.get)
    wanted_ids = np.array([i for i, s in id_to_symbol.items() if s == chosen], dtype=fills["instrument_id"].dtype)
    return chosen, fills[np.isin(fills["instrument_id"], wanted_ids)], per_symbol


def mbo_fill_ticks(path: str | Path, symbol: str | None = None, chunk: int = 5_000_000,
                   log=print) -> list[TradeTick]:
    """Reads an MBO DBN file in chunks, keeps only fills (action F) and builds ticks for one contract.

    With parent symbology (ES.FUT) the file can hold well over 100 million records; filtering
    runs in NumPy, only the fills of the chosen contract are turned into tick objects.
    """
    import databento as db
    import numpy as np
    store = db.DBNStore.from_file(path)
    id_to_symbol: dict[int, str] = {}
    for raw, intervals in store.metadata.mappings.items():
        for iv in intervals:
            id_to_symbol[int(iv["symbol"])] = raw
    parts, total = [], 0
    for k, arr in enumerate(store.to_ndarray(count=chunk), 1):
        total += len(arr)
        parts.append(arr[arr["action"] == b"F"])
        if log and k % 10 == 0:
            log(f"  MBO: {total / 1e6:.0f}M records read, {sum(len(p) for p in parts) / 1e6:.2f}M fills")
    if not parts:
        return []
    fills = np.concatenate(parts)
    chosen, fills, per_symbol = select_fills(fills, id_to_symbol, symbol)
    if log:
        log(f"  MBO {Path(path).name}: {total / 1e6:.1f}M records, fills per contract: "
            + ", ".join(f"{s} {n}" for s, n in sorted(per_symbol.items(), key=lambda kv: -kv[1])[:4]) + f" -> {chosen}")
    return fills_to_ticks(fills, id_to_symbol)


def load_databento(trades_path: str | Path | list[str | Path], definition_path: str | Path | None = None,
                   start: str | None = None, end: str | None = None,
                   symbol: str | None = None) -> tuple[Instrument, list[TradeTick]]:
    """Reads one or more DBN files (schema=trades or mbo) and selects a contract.

    Files with parent symbology (e.g. `ES.FUT`) contain all expiries and spreads.
    The Nautilus loader resolves the symbols via the DBN metadata; then an outright
    contract is chosen: `symbol` (e.g. 'ESH6'), otherwise the one with the most ticks.
    Without a definition file an ES contract with tick 0.25 and multiplier 50 is built.
    Multiple files are merged in timestamp order.
    """
    from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader

    paths = [Path(p) for p in (trades_path if isinstance(trades_path, (list, tuple)) else [trades_path])]
    loader = DatabentoDataLoader()
    definition: Instrument | None = None
    if definition_path:
        # Load definition -> registers instrument(s) in the loader incl. price precision
        instruments = [d for d in loader.from_dbn_file(Path(definition_path))
                       if isinstance(d, Instrument)]
        definition = instruments[0]

    ticks: list[TradeTick] = []
    for p in paths:
        schema = str(inspect_dbn(p)["schema"])
        if schema.endswith("mbo"):
            ticks.extend(mbo_fill_ticks(p, symbol))  # fill granularity like NinjaTrader ticks
        elif schema.endswith("trades"):
            ticks.extend(loader.from_dbn_file(p, price_precision=None if definition else 2,
                                              as_legacy_cython=True))
        else:
            raise ValueError(f"DBN schema is {schema}, expected 'trades' or 'mbo'. File: {p}")
    if not ticks:
        raise ValueError("No ticks in the given files.")

    # Choose contract: outrights only (no '-' in the symbol), requested one or most ticks
    counts = Counter(str(t.instrument_id) for t in ticks)
    outrights = {k: n for k, n in counts.items() if "-" not in k}
    if symbol:
        wanted = symbol if "." in symbol else f"{symbol}.GLBX"
        if wanted not in counts:
            raise ValueError(f"Contract {wanted} not in the data. Available: "
                             + ", ".join(f"{k} ({n})" for k, n in sorted(outrights.items(), key=lambda kv: -kv[1])))
        chosen = wanted
    else:
        chosen = max(outrights, key=outrights.get)
    ticks = [t for t in ticks if str(t.instrument_id) == chosen]
    ticks.sort(key=lambda t: t.ts_init)  # multiple files: ensure ordering

    if definition and str(definition.id) == chosen:
        instrument = definition
    else:
        instrument = es_contract_from_symbol(chosen)

    if start or end:
        import pandas as pd
        s = pd.Timestamp(start, tz="UTC").value if start else 0
        e = pd.Timestamp(end, tz="UTC").value if end else 2**63 - 1
        ticks = [t for t in ticks if s <= t.ts_event <= e]
    return instrument, ticks


def day_file(day: str, data_dir: str | Path, schema: str = "trades") -> Path:
    """Path of the Databento daily file: glbx-mdp3-YYYYMMDD.<schema>.dbn.zst."""
    return Path(data_dir) / f"glbx-mdp3-{day.replace('-', '')}.{schema}.dbn.zst"


def previous_day_files(day: str, data_dir: str | Path, schema: str = "trades", max_back: int = 7,
                       min_ratio: float = 0.1) -> list[Path]:
    """Files back to and including the previous full trading day, oldest first (empty if none).

    Sunday and holiday files (e.g. 1 Jan) only hold the Globex reopen in the evening; they belong
    to the next session and are included, but the search continues past them. A file counts as
    a full trading day if it has at least `min_ratio` of the size of the replay day's file.
    """
    from datetime import date, timedelta
    d = date.fromisoformat(day)
    ref = day_file(day, data_dir, schema)
    min_size = max(1_000_000, int(ref.stat().st_size * min_ratio)) if ref.exists() else 1_000_000
    found: list[Path] = []
    for back in range(1, max_back + 1):
        path = day_file((d - timedelta(days=back)).isoformat(), data_dir, schema)
        if not path.exists():
            continue
        found.insert(0, path)
        if path.stat().st_size >= min_size:
            return found
    return []


def contracts_in(trades_path: str | Path) -> dict[str, int]:
    """Tick count per contract in a file (trades: records, MBO: fills), e.g. for roll days."""
    if str(inspect_dbn(trades_path)["schema"]).endswith("mbo"):
        ticks = mbo_fill_ticks(trades_path)
    else:
        from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
        ticks = DatabentoDataLoader().from_dbn_file(Path(trades_path), price_precision=2)
    return dict(Counter(str(t.instrument_id) for t in ticks).most_common())


def synthetic(instrument: Instrument, n: int = 20_000, seed: int = 1, start_px: float = 6400.0):
    """Random ticks with a slight trend - only for trying out the UI."""
    rnd = random.Random(seed)
    inc = float(instrument.price_increment)
    px = start_px
    t0 = 1_756_000_000_000_000_000
    ticks = []
    drift = 0.0
    ts = t0
    side = AggressorSide.BUYER
    for k in range(n):
        drift = 0.98 * drift + rnd.gauss(0, 0.02)
        new_px = round((px + inc * round(rnd.gauss(drift, 1.0))) / inc) * inc
        # Aggressor from the price direction: Nautilus derives bid/ask from it, random
        # sides would fill limit orders at unrealistic prices.
        if new_px != px:
            side = AggressorSide.BUYER if new_px > px else AggressorSide.SELLER
        px = new_px
        ts += rnd.randint(50, 400) * 1_000_000
        ticks.append(TradeTick(instrument.id, Price(px, instrument.price_precision),
                               Quantity.from_int(rnd.randint(1, 8)), side,
                               TradeId(str(k)), ts, ts))
    return ticks
