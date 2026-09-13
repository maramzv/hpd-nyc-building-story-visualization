"""Phase 4 extension: add a real neighborhood tier (city -> borough -> neighborhood -> building).

1. Pull all NYC Neighborhood Tabulation Area (NTA) boundaries (dataset 9nt8-h7nd).
2. For every building in map_dataset.json, find which NTA polygon contains its
   lat/lon (point-in-polygon via shapely).
3. Add "neighborhood" to each building record.
4. Compute a neighborhood-level aggregate file (pattern breakdown per
   neighborhood, plus a centroid to fly to) for the map UI.

Run: python scripts/add_neighborhoods.py
Reads/writes data/map_dataset.json in place, writes data/neighborhoods.json.
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from shapely.geometry import shape, Point

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from socrata_client import soql_query  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NTA_DATASET_ID = "9nt8-h7nd"


def main():
    print("Pulling NTA neighborhood boundaries...")
    start = time.time()
    nta_rows = soql_query(select="ntaname,boroname,the_geom", limit=300,
                           timeout=120, dataset_id=NTA_DATASET_ID)
    print(f"  {time.time()-start:.1f}s, {len(nta_rows)} neighborhoods")

    # Pre-build shapely polygons once
    ntas = []
    for r in nta_rows:
        geom = r.get("the_geom")
        if not geom:
            continue
        try:
            poly = shape(geom)
        except Exception:
            continue
        ntas.append((r["ntaname"], r["boroname"], poly))
    print(f"  {len(ntas)} valid polygons parsed")

    data = json.load(open(DATA_DIR / "map_dataset.json"))
    print(f"Matching {len(data)} buildings to neighborhoods...")

    matched = 0
    for b in data:
        pt = Point(b["lon"], b["lat"])
        found = None
        for name, boro, poly in ntas:
            if poly.contains(pt):
                found = name
                break
        b["neighborhood"] = found or ""
        if found:
            matched += 1

    print(f"Matched: {matched}/{len(data)} ({matched/len(data)*100:.1f}%)")
    json.dump(data, open(DATA_DIR / "map_dataset.json", "w"))

    # Aggregate per neighborhood: pattern breakdown + centroid to fly to
    by_nbhd = defaultdict(list)
    for b in data:
        if b["neighborhood"]:
            by_nbhd[(b["neighborhood"], b["boro"])].append(b)

    nbhd_agg = []
    for (name, boro), buildings in by_nbhd.items():
        counts = defaultdict(int)
        for b in buildings:
            counts[b["pattern"]] += 1
        lat = sum(b["lat"] for b in buildings) / len(buildings)
        lon = sum(b["lon"] for b in buildings) / len(buildings)
        nbhd_agg.append({
            "neighborhood": name,
            "boro": boro,
            "total": len(buildings),
            "no_real_defects": counts.get("No real defects", 0),
            "isolated": counts.get("Isolated", 0),
            "widespread": counts.get("Widespread", 0),
            "persistent": counts.get("Persistent", 0),
            "chronic": counts.get("Chronic", 0),
            "lat": lat,
            "lon": lon,
        })
    nbhd_agg.sort(key=lambda x: -x["total"])
    json.dump(nbhd_agg, open(DATA_DIR / "neighborhoods.json", "w"))
    print(f"Saved {len(nbhd_agg)} neighborhood aggregates to data/neighborhoods.json")


if __name__ == "__main__":
    main()
