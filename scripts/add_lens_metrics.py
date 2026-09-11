"""Post-process step: add the extra per-building metrics the map's lens picker
needs (Class C count, recent-2yr count, age of the newest open violation) to
data/map_dataset.json.

Same pattern as add_neighborhoods.py / add_footprints.py: read the existing
map_dataset.json, enrich each row, write it back. Source is the already-pulled
violation cache (data/map_dataset_violations_raw.json, ~2.4 GB) - streamed with
ijson so we never hold the whole array in memory. No network calls.

Every row in "Open HPD Violations" (csn4-vhvf) is an open violation, so
len(rows) == active_count and novissueddate is the issue date of a currently
open violation.

Run: .venv/Scripts/python.exe scripts/add_lens_metrics.py
"""
import json
from datetime import datetime
from pathlib import Path

import ijson

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW = DATA_DIR / "map_dataset_violations_raw.json"
MAP = DATA_DIR / "map_dataset.json"
TODAY = datetime(2026, 8, 14)          # same reference "now" as build_map_dataset.py
RECENT_WINDOW_DAYS = 730               # same as building_story.RECENT_WINDOW_DAYS


def parse_date(s):
    if not s or len(s) < 10:
        return None
    try:
        return datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except (ValueError, TypeError):
        return None


def main():
    if not RAW.exists():
        raise SystemExit(f"missing {RAW} - can't compute lens metrics without the violation cache")

    # per-building accumulators (small: ints + one date each)
    class_c = {}          # bid -> count of class C
    recent = {}           # bid -> count issued within RECENT_WINDOW_DAYS
    oldest = {}           # bid -> earliest novissueddate seen (drives backlog age)

    n = 0
    with open(RAW, "rb") as f:
        for v in ijson.items(f, "item"):
            n += 1
            if n % 500_000 == 0:
                print(f"  ...{n:,} violation rows scanned")
            bid = v.get("buildingid")
            if bid is None:
                continue
            bid = str(bid)
            if v.get("class") == "C":
                class_c[bid] = class_c.get(bid, 0) + 1
            d = parse_date(v.get("novissueddate"))
            if d:
                if (TODAY - d).days <= RECENT_WINDOW_DAYS:
                    recent[bid] = recent.get(bid, 0) + 1
                if bid not in oldest or d < oldest[bid]:
                    oldest[bid] = d
    print(f"Scanned {n:,} violation rows across {len(oldest):,} buildings")

    rows = json.load(open(MAP))
    print(f"Enriching {len(rows):,} buildings in {MAP.name}")

    backup = MAP.with_suffix(".json.pre-lens-metrics")
    if not backup.exists():
        backup.write_bytes(MAP.read_bytes())
        print(f"  backed up original to {backup.name}")

    matched = 0
    for r in rows:
        bid = str(r["buildingid"])
        if bid in class_c or bid in recent or bid in oldest:
            matched += 1
        r["class_c_count"] = class_c.get(bid, 0)
        r["recent_count"] = recent.get(bid, 0)
        od = oldest.get(bid)
        # backlog age: years since the OLDEST still-open violation was issued
        r["backlog_years"] = round((TODAY - od).days / 365, 1) if od else 0.0
    print(f"  matched violation data for {matched:,}/{len(rows):,} buildings")

    json.dump(rows, open(MAP, "w"))
    print(f"Wrote {MAP.name}")

    # quick distribution readout so the map can pick sensible height scales
    for key in ("class_c_count", "recent_count", "backlog_years"):
        vals = sorted(r[key] for r in rows)
        def q(p):
            return vals[min(len(vals) - 1, int(p * len(vals)))]
        print(f"  {key:26s} min={vals[0]:>6}  median={q(.5):>6}  "
              f"p90={q(.9):>6}  p99={q(.99):>6}  max={vals[-1]:>6}")


if __name__ == "__main__":
    main()
