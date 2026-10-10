"""Compares an NT8 event CSV (EntryCount/DoubleTrap with ExportCsv) with the ChartTrader port's CSV of the same day.

Rows are matched by BarEndTime + Indicator + Event + Direction; the same key can occur more than once on a bar
(e.g. two DtRejected), so occurrences are numbered in file order. Every other column is compared: prices exactly,
BodyPct/ClosePct/WickPct with a tolerance of 0.5, text exactly. Only the time range both files cover is compared
(an NT file written during a replay ends where the replay was).

    python parity_check.py NT.csv CT.csv [--bars]
    python parity_check.py --day 20261009 [--nt-dir "C:\\Users\\...\\Documents\\NinjaTrader 8"] [--ct-dir pa_csv]

--bars also compares the per-bar lines (count and state of both directions): the first differing Bar line shows
where the two runs part. Exit code 0 = 100 % parity.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

KEY = ("BarEndTime", "Indicator", "Event", "Direction")
PRICE_COLS = {"Open", "High", "Low", "Close", "SignalH", "SignalL", "SignalO", "SignalC", "EMA", "Trigger", "Stop"}
PCT_COLS = {"BodyPct", "ClosePct", "WickPct"}
MAX_LISTED = 200


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln.rstrip("\r\n") for ln in f if ln.strip()]
    header = lines[0].split(";")
    rows = []
    for ln in lines[1:]:
        parts = ln.split(";")
        if len(parts) > len(header):                 # ';' inside the reason would have been replaced, keep it safe
            parts = parts[:len(header) - 1] + [";".join(parts[len(header) - 1:])]
        rows.append(dict(zip(header, parts + [""] * (len(header) - len(parts)))))
    return header, rows


def parse_time(s: str) -> datetime:
    return datetime.strptime(s, "%d.%m.%Y %H:%M:%S")


def num(s: str) -> float | None:
    try:
        return float(s.replace(",", ".")) if s != "" else None
    except ValueError:
        return None


def keyed(rows: list[dict]) -> dict[tuple, dict]:
    seen: dict[tuple, int] = defaultdict(int)
    out = {}
    for r in rows:
        k = tuple(r.get(c, "") for c in KEY)
        seen[k] += 1
        out[k + (seen[k],)] = r
    return out


def diff_row(nt: dict, ct: dict, columns: list[str], pct_tol: float) -> list[str]:
    out = []
    for c in columns:
        if c in KEY:
            continue
        a, b = nt.get(c, ""), ct.get(c, "")
        if a == b:
            continue
        if c in PCT_COLS or c in PRICE_COLS:
            x, y = num(a), num(b)
            tol = pct_tol if c in PCT_COLS else 0.0
            if x is not None and y is not None and abs(x - y) <= tol + 1e-12:
                continue
        out.append(f"{c}: NT={a!r} CT={b!r}")
    return out


def compare(nt_path: Path, ct_path: Path, with_bars: bool, pct_tol: float) -> bool:
    h_nt, nt_rows = read_csv(nt_path)
    h_ct, ct_rows = read_csv(ct_path)
    if h_nt != h_ct:
        print(f"Header differs:\n  NT: {';'.join(h_nt)}\n  CT: {';'.join(h_ct)}")
    columns = [c for c in h_nt if c in h_ct]

    def span(rows):
        ts = [parse_time(r["BarEndTime"]) for r in rows if r.get("BarEndTime")]
        return (min(ts), max(ts)) if ts else (None, None)
    (a0, a1), (b0, b1) = span(nt_rows), span(ct_rows)
    if a0 is None or b0 is None:
        print("A file has no rows.")
        return False
    lo, hi = max(a0, b0), min(a1, b1)

    def pick(rows):
        return [r for r in rows if lo <= parse_time(r["BarEndTime"]) <= hi and (with_bars or r["Event"] != "Bar")]
    nt, ct = keyed(pick(nt_rows)), keyed(pick(ct_rows))

    print(f"NT: {nt_path}\nCT: {ct_path}")
    print(f"Compared range: {lo:%d.%m.%Y %H:%M:%S} - {hi:%d.%m.%Y %H:%M:%S}"
          + ("" if with_bars else " (events only, --bars for the per-bar lines)"))
    only_nt = sorted(set(nt) - set(ct), key=lambda k: (parse_time(k[0]), k))
    only_ct = sorted(set(ct) - set(nt), key=lambda k: (parse_time(k[0]), k))
    common = sorted(set(nt) & set(ct), key=lambda k: (parse_time(k[0]), k))
    differing = []
    for k in common:
        d = diff_row(nt[k], ct[k], columns, pct_tol)
        if d:
            differing.append((k, d))
    matches = len(common) - len(differing)
    total = len(set(nt) | set(ct))

    print(f"Matches: {matches} of {total}   only NT: {len(only_nt)}   only CT: {len(only_ct)}   "
          f"same key, different columns: {len(differing)}")

    def label(k):
        r = nt.get(k) or ct.get(k)
        no = f"bar {r.get('BarNo')}" if r.get("BarNo") else ""
        occ = f" #{k[4]}" if k[4] > 1 else ""
        return f"{k[0]} {no} {k[2]} {k[3]}{occ}".replace("  ", " ")

    events = [("only NT", k, nt[k].get("Reason", "")) for k in only_nt] + \
             [("only CT", k, ct[k].get("Reason", "")) for k in only_ct]
    events.sort(key=lambda e: (parse_time(e[1][0]), e[1]))
    for kind, k, reason in events[:MAX_LISTED]:
        print(f"  {kind:8} {label(k)}  {reason}")
    for k, d in differing[:MAX_LISTED]:
        print(f"  differs  {label(k)}")
        for line in d:
            print(f"           {line}")
    if len(events) + len(differing) > 2 * MAX_LISTED:
        print(f"  ... (lists cut at {MAX_LISTED} lines each)")
    ok = matches == total
    print("PARITY 100 %" if ok else f"PARITY {matches / total * 100:.1f} %" if total else "No rows to compare.")
    return ok


def find(dir_: Path, indicator: str, day: str, suffix: str) -> Path | None:
    hits = sorted(dir_.glob(f"{indicator}_*_{day}{suffix}.csv"))
    return hits[-1] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Parity of NT8 and ChartTrader event CSVs")
    ap.add_argument("files", nargs="*", help="NT.csv CT.csv")
    ap.add_argument("--day", help="yyyyMMdd: compare EntryCount and DoubleTrap of this trading day")
    ap.add_argument("--nt-dir", default=str(Path.home() / "Documents" / "NinjaTrader 8"))
    ap.add_argument("--ct-dir", default=str(Path(__file__).with_name("pa_csv")))
    ap.add_argument("--suffix", default="_CT")
    ap.add_argument("--bars", action="store_true", help="also compare the per-bar lines")
    ap.add_argument("--pct-tol", type=float, default=0.5, help="tolerance of BodyPct/ClosePct/WickPct")
    args = ap.parse_args()

    pairs: list[tuple[Path, Path]] = []
    if args.day:
        for ind in ("EntryCount", "DoubleTrap"):
            nt = next((p for p in sorted(Path(args.nt_dir).glob(f"{ind}_*_{args.day}.csv"))), None)
            ct = find(Path(args.ct_dir), ind, args.day, args.suffix)
            if nt is None or ct is None:
                print(f"{ind} {args.day}: {'NT file missing' if nt is None else ''} {'CT file missing' if ct is None else ''}")
                continue
            pairs.append((nt, ct))
    elif len(args.files) == 2:
        pairs.append((Path(args.files[0]), Path(args.files[1])))
    else:
        ap.error("give NT.csv CT.csv or --day yyyyMMdd")
    if not pairs:
        return 1
    ok = True
    for nt, ct in pairs:
        ok &= compare(nt, ct, args.bars, args.pct_tol)
        print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
