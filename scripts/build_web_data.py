"""Pack the enriched map_dataset.json + footprints.json into the small, split
files the website actually fetches (data/web/).

This is the final *publish* step of the pipeline. Everything upstream
(build_map_dataset -> apply_geocodes -> add_lens_metrics -> add_neighborhoods,
plus add_footprints) keeps writing the same canonical intermediates it always
has; they stay gitignored. Only the files this script emits are served.

Why this step exists:

1. Size. map_dataset.json is 166k rows that all share the same 14 keys, so
   ~34MB of its 55MB is the key names repeated 166,481 times. Writing it
   columnar (parallel arrays + small dictionaries for the low-cardinality
   fields) drops it to ~14MB, and splitting per borough keeps every single
   file well under GitHub's 50MB warning threshold.

2. Correctness under progressive loading. Two things the browser used to
   compute across the *whole* dataset are precomputed here instead, because
   neither survives loading the boroughs one at a time:

     - the per-lens maxima every bar height is normalised against (load
       Brooklyn first and Queens later and every height would silently
       rebase mid-session), and
     - the overlapping-coordinate grid jitter, which groups buildings by
       exact shared coordinate - 70 of those groups span more than one
       borough, so doing it per-file would not match today's output.

   Precomputing both makes the render deterministic and independent of the
   order slices arrive in, and removes two O(n) passes from every page load.

Run: python scripts/build_web_data.py
"""

import json
import math
import shutil
from collections import defaultdict
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "web"

# Fixed display order, mirrored in the frontend. Stored as indices into these
# lists rather than as repeated strings.
SCALES = ["Minimal", "Low", "Moderate", "Large", "Severe"]
PATTERNS = ["No real defects", "Isolated", "Widespread", "Persistent", "Chronic"]

# Lenses whose bar heights are normalised against the dataset-wide worst
# building. ("age" is excluded on purpose: it uses a fixed per-year multiplier
# instead, so its max is never read.)
NORMALISED_LENSES = {
    "columns": lambda r: r["active_count"],
    "classc": lambda r: r["class_c_count"],
    "recent": lambda r: r["recent_count"],
}

METERS_PER_DEG_LAT = 111320


def deduplicate_overlapping_coordinates(rows):
    """Spread buildings that share an exact coordinate into a small grid.

    A faithful port of the deduplicateOverlappingCoordinates() that used to run
    in map.html on every page load - same grouping key, same spacing formula,
    same "first one keeps its real coordinate" rule, and the same use of the
    already-updated latitude when converting the longitude offset. Mutates
    rows in place and returns (collisions, largest_group) for reporting.
    """
    groups = defaultdict(list)
    for r in rows:
        groups["{:.6f},{:.6f}".format(r["lat"], r["lon"])].append(r)

    collisions = 0
    largest_group = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        collisions += len(group)
        largest_group = max(largest_group, len(group))
        # Spacing scales with the group's own largest footprint - a fixed
        # value badly overlaps superblock developments (Co-op City, ~250
        # BuildingIDs on one lot) whose buildings are 50m across on their own.
        max_half_width = max(math.sqrt(r["footprint_m2"] or 150) / 2 for r in group)
        spacing = max_half_width * 2 + 4
        cols = math.ceil(math.sqrt(len(group)))
        n_rows = math.ceil(len(group) / cols)
        for i, r in enumerate(group):
            if i == 0:
                continue  # keeps the real, unmodified coordinate
            col = i % cols
            row = i // cols
            offset_x = (col - (cols - 1) / 2) * spacing
            offset_y = (row - (n_rows - 1) / 2) * spacing
            r["lat"] += offset_y / METERS_PER_DEG_LAT
            r["lon"] += offset_x / (
                METERS_PER_DEG_LAT * math.cos(r["lat"] * math.pi / 180)
            )
    return collisions, largest_group


def slug(boro):
    return boro.lower().replace(" ", "-")


def pack_buildings(rows):
    """Columnar form of one borough's buildings.

    Coordinates keep 6 decimals (~0.1m): the grid jitter above is metres-scale
    and rounding harder would visibly move buildings. Everything else is
    rounded to the precision the UI actually displays.
    """
    neighborhoods = sorted({r["neighborhood"] for r in rows})
    n_index = {n: i for i, n in enumerate(neighborhoods)}
    s_index = {v: i for i, v in enumerate(SCALES)}
    p_index = {v: i for i, v in enumerate(PATTERNS)}
    return {
        "n": len(rows),
        "neighborhoods": neighborhoods,
        "buildingid": [r["buildingid"] for r in rows],
        "address": [r["address"] for r in rows],
        "lat": [round(r["lat"], 6) for r in rows],
        "lon": [round(r["lon"], 6) for r in rows],
        "floors": [r["floors"] for r in rows],
        "footprint_m2": [round(r["footprint_m2"], 1) for r in rows],
        "active_count": [r["active_count"] for r in rows],
        "scale": [s_index[r["scale"]] for r in rows],
        "pattern": [p_index[r["pattern"]] for r in rows],
        "neighborhood": [n_index[r["neighborhood"]] for r in rows],
        "class_c_count": [r["class_c_count"] for r in rows],
        "recent_count": [r["recent_count"] for r in rows],
        "backlog_years": [round(r["backlog_years"], 1) for r in rows],
    }


def write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    mb = path.stat().st_size / 1e6
    print("  {}  {:.1f}MB".format(path.relative_to(DATA.parent), mb))
    return mb


def main():
    print("loading map_dataset.json ...")
    with open(DATA / "map_dataset.json", encoding="utf-8") as f:
        buildings = json.load(f)
    print("  {:,} buildings".format(len(buildings)))

    collisions, largest = deduplicate_overlapping_coordinates(buildings)
    if collisions:
        print(
            "  {:,} buildings shared a coordinate with at least one other "
            "(largest group: {}); spread into a grid".format(collisions, largest)
        )

    # Dataset-wide lens maxima, computed once here instead of per page load.
    lens_max = {
        key: max((metric(r) or 0) for r in buildings) or 1
        for key, metric in NORMALISED_LENSES.items()
    }

    by_boro = defaultdict(list)
    for r in buildings:
        by_boro[r["boro"]].append(r)

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    print("writing buildings ...")
    total = 0.0
    boro_meta = {}
    for boro in sorted(by_boro):
        rows = by_boro[boro]
        counts = defaultdict(int)
        for r in rows:
            counts[r["pattern"]] += 1
        total += write(OUT / "buildings-{}.json".format(slug(boro)), pack_buildings(rows))
        boro_meta[boro] = {
            "count": len(rows),
            "patterns": {p: counts.get(p, 0) for p in PATTERNS},
            "file": "buildings-{}.json".format(slug(boro)),
        }

    # Footprints are a separate, heavier payload fetched only when the
    # Buildings lens is shown, so they are split the same way but never
    # loaded as part of the initial render.
    print("writing footprints ...")
    fp_path = DATA / "footprints.json"
    fp_meta = {}
    if fp_path.exists():
        boro_of = {r["buildingid"]: r["boro"] for r in buildings}
        fp_by_boro = defaultdict(list)
        orphans = 0
        with open(fp_path, encoding="utf-8") as f:
            footprints = json.load(f)
        for fp in footprints:
            boro = boro_of.get(fp["buildingid"])
            if boro is None:
                orphans += 1
                continue
            fp_by_boro[boro].append(fp)
        if orphans:
            print("  note: {:,} footprints had no matching building; skipped".format(orphans))
        for boro in sorted(fp_by_boro):
            rows = fp_by_boro[boro]
            total += write(
                OUT / "footprints-{}.json".format(slug(boro)),
                {
                    "buildingid": [r["buildingid"] for r in rows],
                    "height_m": [r["height_m"] for r in rows],
                    "rings": [r["rings"] for r in rows],
                },
            )
            fp_meta[boro] = {
                "count": len(rows),
                "file": "footprints-{}.json".format(slug(boro)),
            }
    else:
        print("  footprints.json not found; skipping (map falls back to metric lenses)")

    pattern_counts = defaultdict(int)
    for r in buildings:
        pattern_counts[r["pattern"]] += 1

    print("writing summary ...")
    total += write(
        OUT / "summary.json",
        {
            "generated": date.today().isoformat(),
            "total": len(buildings),
            "scales": SCALES,
            "patterns": PATTERNS,
            "pattern_counts": {p: pattern_counts.get(p, 0) for p in PATTERNS},
            "boroughs": boro_meta,
            "footprints": fp_meta,
            # Bar-height normalisation. lens_top_m mirrors the old
            # LENS_TOP_M = LENSES.columns.max * 5.
            "lens_max": lens_max,
            "lens_top_m": lens_max["columns"] * 5,
        },
    )

    print("\ndone -> {}  ({:.1f}MB across {} files)".format(
        OUT, total, len(list(OUT.iterdir()))))


if __name__ == "__main__":
    main()
