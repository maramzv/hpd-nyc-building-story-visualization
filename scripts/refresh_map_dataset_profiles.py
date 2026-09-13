"""Fast path for re-running just the story-profile logic (pattern, narrative,
backlog_age, etc.) over the existing map_dataset.json,
without redoing the PLUTO join (lat/lon/floors/footprint) that
build_map_dataset.py always repeats from scratch.

Use this after a change to src/building_story.py that's pure logic/wording
and doesn't touch anything location-related - it reuses the raw violation
cache and the coordinates already sitting in data/map_dataset.json instead
of re-querying PLUTO's ~840 batches for all 167k buildings.

If you've added/changed which buildings are included, or need fresh
coordinates, use build_map_dataset.py instead.

Run: python scripts/refresh_map_dataset_profiles.py
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from building_story import build_profile  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TODAY = datetime(2026, 8, 14)


def main():
    dataset_path = DATA_DIR / "map_dataset.json"
    raw_path = DATA_DIR / "map_dataset_violations_raw.json"

    dataset = json.load(open(dataset_path, encoding="utf-8"))
    print(f"Loaded {len(dataset)} existing buildings from {dataset_path.name}")

    raw_rows = json.load(open(raw_path, encoding="utf-8"))
    by_building = defaultdict(list)
    for r in raw_rows:
        by_building[r["buildingid"]].append(r)
    print(f"Loaded violation records for {len(by_building)} buildings from cache")

    updated = 0
    missing = 0
    for b in dataset:
        viols = by_building.get(b["buildingid"])
        if not viols:
            missing += 1
            continue
        p = build_profile(b["buildingid"], viols, TODAY)
        # recency/severity/engagement/backlog_age/narrative deliberately
        # excluded - see the matching comment in build_map_dataset.py.
        b["active_count"] = p.active_count
        b["scale"] = p.scale
        b["pattern"] = p.pattern
        b.pop("long_unresolved", None)
        b.pop("recency", None)
        b.pop("severity", None)
        b.pop("engagement", None)
        b.pop("backlog_age", None)
        b.pop("narrative", None)
        updated += 1

    print(f"Refreshed profiles for {updated} buildings ({missing} had no cached violation records, left unchanged)")
    json.dump(dataset, open(dataset_path, "w"))
    print(f"Saved to {dataset_path}")
    print("\nIf `pattern` values changed for any building, also rerun add_neighborhoods.py "
          "to refresh the neighborhood aggregate counts.")


if __name__ == "__main__":
    main()
