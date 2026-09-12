"""Post-process step: add the extra per-building metrics the map's lens picker
needs (Class C count, recent-2yr count, years past deadline for the oldest
open backlog violation) to data/map_dataset.json.

Same pattern as add_neighborhoods.py / add_footprints.py: read the existing
map_dataset.json, enrich each row, write it back. Source is the already-pulled
violation cache (data/map_dataset_violations_raw.json, ~2.4 GB) - streamed with
ijson so we never hold the whole array in memory. No network calls.

backlog_years mirrors building_story's max_years_overdue (see building_story.js/py):
years since the oldest correction deadline still missed by a violation that
hasn't been certified. This keeps the map's Age lens, the building panel's
backlog metric, and the story sentence all reporting the same number for the
same building instead of three different definitions of "age".

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
ACCEPTED_CERT_STATUSES = {"NOV CERTIFIED ON TIME", "NOV CERTIFIED LATE"}  # same as building_story


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
    max_overdue_days = {}  # bid -> days past deadline for the oldest-missed-deadline open violation

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
            if d and (TODAY - d).days <= RECENT_WINDOW_DAYS:
                recent[bid] = recent.get(bid, 0) + 1
            # Same definition as building_story's max_years_overdue: the oldest
            # correction deadline still missed by a violation that hasn't been
            # certified - not "oldest violation ever issued". Keeps the map's
            # Age lens, the backlog metric, and the story sentence all reading
            # the same number for the same building.
            certified = v.get("currentstatus") in ACCEPTED_CERT_STATUSES
            deadline = parse_date(v.get("newcorrectbydate")) or parse_date(v.get("originalcorrectbydate"))
            if deadline and deadline < TODAY and not certified:
                days_overdue = (TODAY - deadline).days
                if days_overdue > max_overdue_days.get(bid, 0):
                    max_overdue_days[bid] = days_overdue
    print(f"Scanned {n:,} violation rows across {len(max_overdue_days):,} buildings")

    rows = json.load(open(MAP))
    print(f"Enriching {len(rows):,} buildings in {MAP.name}")

    backup = MAP.with_suffix(".json.pre-lens-metrics")
    if not backup.exists():
        backup.write_bytes(MAP.read_bytes())
        print(f"  backed up original to {backup.name}")

    matched = 0
    for r in rows:
        bid = str(r["buildingid"])
        if bid in class_c or bid in recent or bid in max_overdue_days:
            matched += 1
        r["class_c_count"] = class_c.get(bid, 0)
        r["recent_count"] = recent.get(bid, 0)
        # backlog age: years past deadline for the oldest still-open, uncertified violation
        r["backlog_years"] = round(max_overdue_days.get(bid, 0) / 365, 1)
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
