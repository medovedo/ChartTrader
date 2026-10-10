"""Counts the events of EntryCount/DoubleTrap CSVs per trading day, for the "effect" column of CHANGELOG_Tuning.md.

    python pa_stats.py pa_csv                         # one table: all days in the folder
    python pa_stats.py pa_csv/baseline pa_csv         # before -> after, with the difference
    python pa_stats.py pa_csv --days 20261006 20261009 --indicator EntryCount

Counted (EntryCount): entries per count and direction (1EL, 2EL, 3EL+, 1ES, ...; skipped entries not included),
quality ok among the 2E, F2EL/F2ES (the "no same-bar F2E" notes not included), resets per reason.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


def read_events(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        header = f.readline().rstrip("\r\n").split(";")
        return [dict(zip(header, ln.rstrip("\r\n").split(";"))) for ln in f if ln.strip()]


def count(rows: list[dict]) -> Counter:
    c: Counter = Counter()
    for r in rows:
        ev, d = r["Event"], r["Direction"][:1]          # L / S
        if ev == "Entry" and not r["Reason"].startswith("skipped"):
            n = int(r["Count"])
            c[f"{min(n, 3)}{'+' if n >= 3 else ''}E{d}"] += 1
            if n == 2 and r["QualityOk"] == "true":
                c[f"2E{d} quality ok"] += 1
        elif ev == "Entry":
            c[f"skipped E{d}"] += 1
        elif ev == "F2E" and not r["Reason"].startswith("no same-bar"):
            c[f"F2E{d}"] += 1
        elif ev == "Reset":
            c[f"Reset {r['Reason'].split(',')[0]} {d}"] += 1
        elif ev not in ("Bar", "F2E"):
            c[f"{ev} {d}"] += 1
    return c


ORDER = ["1EL", "2EL", "2EL quality ok", "3+EL", "F2EL", "1ES", "2ES", "2ES quality ok", "3+ES", "F2ES",
         "skipped EL", "skipped ES"]


def collect(folder: Path, indicator: str, days: list[str] | None) -> dict[str, Counter]:
    out = {}
    for p in sorted(folder.glob(f"{indicator}_*.csv")):
        day = next((part[:8] for part in p.stem.split("_") if part[:8].isdigit() and len(part) >= 8), None)
        if day is None or (days and day not in days):
            continue
        out[day] = count(read_events(p))
    return out


def table(stats: dict[str, Counter], before: dict[str, Counter] | None = None) -> None:
    keys = [k for k in ORDER if any(k in c for c in stats.values()) or (before and any(k in c for c in before.values()))]
    keys += sorted({k for c in stats.values() for k in c} - set(keys))
    days = sorted(stats)
    print(f"{'':16}" + "".join(f"{d:>14}" for d in days) + f"{'total':>14}")
    for k in keys:
        cells = []
        tot_a = tot_b = 0
        for d in days:
            a = stats[d][k]
            tot_a += a
            if before is not None:
                b = before.get(d, Counter())[k]
                tot_b += b
                cells.append(f"{b}->{a}" if a != b else str(a))
            else:
                cells.append(str(a))
        if before is not None:
            cells.append(f"{tot_b}->{tot_a}" if tot_a != tot_b else str(tot_a))
        else:
            cells.append(str(tot_a))
        print(f"{k:16}" + "".join(f"{c:>14}" for c in cells))


def main() -> int:
    ap = argparse.ArgumentParser(description="Event counts of EntryCount/DoubleTrap CSVs")
    ap.add_argument("folders", nargs="+", help="one folder, or two: before after")
    ap.add_argument("--indicator", default="EntryCount", choices=["EntryCount", "DoubleTrap"])
    ap.add_argument("--days", nargs="*", help="yyyyMMdd (default: all)")
    args = ap.parse_args()
    if len(args.folders) > 2:
        ap.error("one or two folders")
    stats = [collect(Path(f), args.indicator, args.days) for f in args.folders]
    if not stats[-1]:
        sys.exit(f"No {args.indicator} CSVs in {args.folders[-1]}")
    print(f"{args.indicator}: {args.folders[-1]}" + (f"  (before: {args.folders[0]})" if len(stats) == 2 else ""))
    table(stats[-1], stats[0] if len(stats) == 2 else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
