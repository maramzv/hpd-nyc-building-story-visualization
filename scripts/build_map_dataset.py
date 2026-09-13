"""Phase 4, step 1: build the dataset that feeds the 3D map visualization.

1. Take every building citywide that has at least one open HPD violation
   (~167,857 buildings - the full population, not a sample). IDs are
   shuffled with a fixed seed before batching so buildings with very high
   violation counts (some have 2,000+) get spread evenly across batches
   instead of clustering and risking the per-query row cap.
2. Pull their violation records (batched - too many buildingids for one
   query) and compute each one's six-dimension story profile.
3. Join each building to PLUTO (NYC's tax-lot dataset) for lat/lon and
   real floor count via borough+block+lot (batched, verified ~97.5% match
   rate in Phase 3 testing).
4. Export one JSON file: one row per successfully-profiled, successfully-
   located building, ready for the deck.gl/MapLibre frontend to read directly.

The violation pull is checkpointed batch-by-batch to a .jsonl file so a
30-60+ minute run can be killed and resumed without re-fetching completed
batches - see PARTIAL_PATH/PROGRESS_PATH below.

Run: python scripts/build_map_dataset.py
"""
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from socrata_client import soql_query  # noqa: E402
from building_story import build_profile  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TODAY = datetime(2026, 8, 14)
BATCH_SIZE = 250
PLUTO_DATASET_ID = "64uk-42ks"
BORO_MAP = {"MANHATTAN": "MN", "BRONX": "BX", "BROOKLYN": "BK", "QUEENS": "QN", "STATEN ISLAND": "SI"}

# Only the fields build_profile(), the PLUTO join, and the final assembly
# actually use - trimming the rest keeps the full ~2.9M-row cache manageable
# in memory and on disk (select="*" on the wire is unchanged; this just
# drops unused columns before caching).
NEEDED_FIELDS = [
    "buildingid", "boro", "housenumber", "streetname", "apartment",
    "novdescription", "novissueddate", "class", "currentstatus",
    "newcorrectbydate", "originalcorrectbydate", "ordernumber", "novid",
    "block", "lot",
    # Process-timeline fields (Findings 8/9): let the story distinguish a
    # violation that's actively moving through the process from one that's
    # been frozen at "NOV SENT OUT" for years. currentstatusdate is the
    # load-bearing one; the rest give the full inspection->certify arc.
    "inspectiondate", "approveddate", "originalcertifybydate",
    "newcertifybydate", "certifieddate", "currentstatusdate",
]


def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_current_building_ids(resuming=False):
    """Every distinct buildingid that has an open violation RIGHT NOW, pulled
    live and paginated. Replaces the old frozen list read from
    threshold_calibration_raw.json (2026-08-13) - that list could only ever
    lose buildings on a refresh, never pick up ones whose first violation
    landed after it was captured.

    Every run pulls this live so the dataset is always the current citywide
    population, not a slice of whatever we happened to grab last time. The
    building_ids_current.json cache is ONLY reused mid-run, to keep a killed
    pull's resume batching aligned with the list it started from."""
    cache = DATA_DIR / "building_ids_current.json"
    if resuming and cache.exists():
        ids = json.load(open(cache))
        print(f"Resuming: reusing the {len(ids)}-ID list this pull started from ({cache.name})")
        return ids

    ids, offset, page = [], 0, 50000
    while True:
        rows = query_with_retry(select="buildingid", group="buildingid",
                                order="buildingid", limit=page, offset=offset, timeout=120)
        ids.extend(r["buildingid"] for r in rows if r.get("buildingid"))
        print(f"  building-id page @offset {offset}: +{len(rows)} (total {len(ids)})")
        if len(rows) < page:
            break
        offset += page
    ids = sorted(set(ids))
    json.dump(ids, open(cache, "w"))
    print(f"Pulled {len(ids)} distinct buildings with an open violation (live), cached to {cache.name}")
    return ids


def query_with_retry(retries=5, backoff=3.0, **kwargs):
    """soql_query wrapper that retries transient network failures (seen in
    practice: connection resets from NYC's API partway through an 840-batch
    PLUTO join) instead of letting one blip kill a 60-90 minute run."""
    for attempt in range(retries):
        try:
            return soql_query(**kwargs)
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = backoff * (2 ** attempt)
            print(f"    request failed ({e.__class__.__name__}: {e}), "
                  f"retrying in {wait:.0f}s...")
            time.sleep(wait)


def main():
    random.seed(42)  # reproducible shuffle

    cache_path = DATA_DIR / "map_dataset_violations_raw.json"
    partial_path = DATA_DIR / "map_dataset_violations_raw.partial.jsonl"
    progress_path = DATA_DIR / "map_dataset_violations_raw.progress.json"

    # --refresh (or REFRESH=1) forces a fresh violation pull even if the cache
    # exists - needed when NEEDED_FIELDS changes, since the cache only holds
    # whatever fields were requested when it was last written. The old cache is
    # renamed to *.superseded rather than deleted, so it can be restored by hand.
    force_refresh = "--refresh" in sys.argv or os.environ.get("REFRESH") == "1"
    if cache_path.exists() and force_refresh:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        superseded = cache_path.with_suffix(f".json.superseded-{stamp}")
        cache_path.rename(superseded)
        print(f"--refresh: moved existing cache aside to {superseded.name}, re-pulling")

    if cache_path.exists():
        all_rows = json.load(open(cache_path))
        print(f"Loaded {len(all_rows)} violation rows from cache (skipping re-pull)")
        missing = [f for f in NEEDED_FIELDS if all_rows and f not in all_rows[0]]
        if missing:
            print(f"  WARNING: cache is missing {missing} - run with --refresh to re-pull")
    else:
        resuming = progress_path.exists() and partial_path.exists()
        all_ids = get_current_building_ids(resuming=resuming)
        random.shuffle(all_ids)
        batches = list(chunked(all_ids, BATCH_SIZE))
        print(f"Pulling violations for all {len(all_ids)} citywide buildings "
              f"with an open violation, in {len(batches)} batches")

        start_batch = 0
        if resuming:
            start_batch = json.load(open(progress_path))["completed_batches"]
            print(f"Resuming from batch {start_batch+1}/{len(batches)} "
                  f"(found existing checkpoint)")

        with open(partial_path, "a" if start_batch else "w") as pf:
            for i in range(start_batch, len(batches)):
                batch = batches[i]
                where = "buildingid IN(" + ",".join(f"'{b}'" for b in batch) + ")"
                start = time.time()
                rows = query_with_retry(select="*", where=where, limit=20000, timeout=180)
                elapsed = time.time() - start
                if len(rows) >= 19000:
                    print(f"  batch {i+1}: WARNING {len(rows)} rows, near the "
                          f"20000 row limit - possible truncation")
                for r in rows:
                    pf.write(json.dumps({k: r.get(k) for k in NEEDED_FIELDS}) + "\n")
                pf.flush()
                json.dump({"completed_batches": i + 1, "total_batches": len(batches)},
                           open(progress_path, "w"))
                if (i + 1) % 10 == 0 or i == len(batches) - 1:
                    print(f"  batch {i+1}/{len(batches)}: {elapsed:.1f}s, {len(rows)} rows")

        all_rows = [json.loads(line) for line in open(partial_path)]
        json.dump(all_rows, open(cache_path, "w"))
        partial_path.unlink()
        progress_path.unlink()
    print(f"Total violation rows: {len(all_rows)}")

    by_building = defaultdict(list)
    for r in all_rows:
        by_building[r["buildingid"]].append(r)
    print(f"Buildings with at least one violation record: {len(by_building)}")

    # --- Step 2: compute story profiles ---
    profiles = {}
    for bid, viols in by_building.items():
        p = build_profile(bid, viols, TODAY)
        profiles[bid] = p
    print(f"Profiles computed: {len(profiles)}")

    # --- Step 3: PLUTO join for coordinates + floors, batched ---
    building_loc = {}  # buildingid -> (boro, block, lot)
    for bid, viols in by_building.items():
        v = viols[0]
        boro = BORO_MAP.get(v.get("boro"))
        block, lot = v.get("block"), v.get("lot")
        if boro and block and lot and block != "0" and lot != "0":
            building_loc[bid] = (boro, block, lot)

    loc_items = list(building_loc.items())
    pluto_partial_path = DATA_DIR / "map_dataset_pluto_raw.partial.jsonl"
    pluto_progress_path = DATA_DIR / "map_dataset_pluto_raw.progress.json"
    pluto_batches = list(chunked(loc_items, 200))

    pluto_start_batch = 0
    if pluto_progress_path.exists() and pluto_partial_path.exists():
        pluto_start_batch = json.load(open(pluto_progress_path))["completed_batches"]
        print(f"Resuming PLUTO join from batch {pluto_start_batch+1}/{len(pluto_batches)} "
              f"(found existing checkpoint)")

    with open(pluto_partial_path, "a" if pluto_start_batch else "w") as pf:
        for i in range(pluto_start_batch, len(pluto_batches)):
            batch = pluto_batches[i]
            clauses = " OR ".join(f"(borough='{boro}' AND block={block} AND lot={lot})"
                                   for _, (boro, block, lot) in batch)
            start = time.time()
            rows = query_with_retry(select="borough,block,lot,latitude,longitude,numfloors,bldgarea",
                                     where=clauses, limit=500, timeout=120, dataset_id=PLUTO_DATASET_ID)
            if (i + 1) % 20 == 0 or i == len(pluto_batches) - 1:
                print(f"  pluto batch {i+1}/{len(pluto_batches)}: {time.time()-start:.1f}s, {len(rows)} rows")
            for r in rows:
                pf.write(json.dumps(r) + "\n")
            pf.flush()
            json.dump({"completed_batches": i + 1, "total_batches": len(pluto_batches)},
                       open(pluto_progress_path, "w"))

    pluto_matches = {}  # (boro, block, lot) -> pluto row
    for line in open(pluto_partial_path):
        r = json.loads(line)
        key = (r["borough"], str(r["block"]), str(r["lot"]))
        if key not in pluto_matches:
            pluto_matches[key] = r
    pluto_partial_path.unlink()
    pluto_progress_path.unlink()

    matched = 0
    for bid, (boro, block, lot) in building_loc.items():
        key = (boro, str(block), str(lot))
        if key in pluto_matches:
            matched += 1
    print(f"PLUTO matches: {matched}/{len(building_loc)}")

    # Geocoded per-address points (scripts/geocode_buildings.py), preferred
    # over PLUTO's one-point-per-tax-lot centroid so buildings sit where they
    # actually are - and distinct addresses on one lot get distinct points
    # instead of being spread into an artificial grid client-side.
    geo_path = DATA_DIR / "building_geocodes.json"
    geocodes = json.load(open(geo_path)) if geo_path.exists() else {}
    if geocodes:
        print(f"Loaded {len(geocodes)} geocoded points (preferred over PLUTO centroids)")
    else:
        print("No building_geocodes.json - using PLUTO lot centroids "
              "(run scripts/geocode_buildings.py for real per-address placement)")

    # --- Step 4: assemble final dataset ---
    output = []
    for bid, p in profiles.items():
        loc = building_loc.get(bid)
        pluto = pluto_matches.get((loc[0], str(loc[1]), str(loc[2]))) if loc else None

        geo = geocodes.get(bid)
        lat = lon = None
        if geo:
            lon, lat = float(geo[0]), float(geo[1])
        elif pluto and pluto.get("latitude") and pluto.get("longitude"):
            try:
                lat, lon = float(pluto["latitude"]), float(pluto["longitude"])
            except (ValueError, TypeError):
                lat = lon = None
        if lat is None or lon is None:
            continue  # no usable position from either source

        try:
            floors = float((pluto or {}).get("numfloors") or 0) or 3.0
        except (ValueError, TypeError):
            floors = 3.0

        # Estimate each building's footprint (for map rendering) from PLUTO's
        # total building area divided by floor count, converted sq ft -> m^2.
        # Falls back to a typical NYC row-house footprint when bldgarea is
        # missing/zero, and is clamped so bad data can't produce an absurd
        # or invisible shape on the map.
        try:
            bldgarea_sqft = float((pluto or {}).get("bldgarea") or 0)
        except (ValueError, TypeError):
            bldgarea_sqft = 0
        footprint_m2 = (bldgarea_sqft / floors * 0.0929) if bldgarea_sqft else 150.0
        footprint_m2 = max(30.0, min(2500.0, footprint_m2))

        # recency/severity/engagement/backlog_age/narrative are deliberately
        # NOT included here even though build_profile() computes them - the
        # map only ever reads those from the live per-building fetch
        # (buildProfile()/generateNarrative() run client-side after a click,
        # see showDetail() in map.html), never from this batch file. Keeping
        # them here was costing 58% of the file's size (narrative alone was
        # 43%) for data nothing on the page actually displays.
        output.append({
            "buildingid": bid,
            "address": p.address,
            "boro": by_building[bid][0].get("boro", ""),
            "lat": lat,
            "lon": lon,
            "floors": floors,
            "footprint_m2": footprint_m2,
            "active_count": p.active_count,
            "scale": p.scale,
            "pattern": p.pattern,
        })

    out_path = DATA_DIR / "map_dataset.json"
    json.dump(output, open(out_path, "w"))
    print(f"\nFinal dataset: {len(output)} buildings ready for the map")
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
