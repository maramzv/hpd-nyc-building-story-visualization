"""Phase 4, step 2: attach a real NYC building-footprint polygon + roof height
to every building in map_dataset.json, so the map can render true building
geometry as an alternative to the centroid square columns.

Output: data/footprints.json
    [{ "buildingid", "height_m", "base_elev_m", "source": "bin"|"bbl",
       "rings": [ [ [lng,lat], ... ], ... ] }]      # outer rings, holes dropped
Keyed for a client-side join on buildingid. Kept as a SEPARATE file (not merged
into map_dataset.json) because footprint geometry barely changes year to year,
while the violation-derived fields refresh daily.

Join
----
1. map_dataset.json                       -> every rendered buildingid (~166k)
2. HPD "Buildings Subject to HPD Jurisdiction" (kj4p-ruqc), keyed by the same
   buildingid HPD violations use            -> bin + block + lot
   BBL is reconstructed as boroid(1) + block(5) + lot(4).
3. NYC Building Footprints (5zhs-2jue) - the city's official footprint layer,
   MultiPolygon geometry + HEIGHT_ROOF (ft) + GROUND_ELEVATION (ft), keyed by
   BIN and base_bbl.
       join: BIN first, then base_bbl / mappluto_bbl. When a BBL maps to
       several footprints, keep the largest-area one.
   Height = HEIGHT_ROOF * 0.3048; if missing / <= 3 m, fall back to floors*3.5.

Everything is checkpointed to data/_fp_build_cache/ so a killed run resumes.
`5zhs-2jue` is the API/GeoJSON-enabled resource; the commonly-cited `syp8-uezg`
is a non-API map view and 404s the query endpoint.

Run:  .venv/Scripts/python.exe scripts/add_footprints.py
"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from socrata_client import soql_query  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "_fp_build_cache"
CACHE.mkdir(exist_ok=True)
OUT = DATA / "footprints.json"

HPD_BUILDINGS = "kj4p-ruqc"
FOOTPRINTS = "5zhs-2jue"
FT_TO_M = 0.3048
SIMPLIFY_M = 0.90   # drop a vertex within this of the segment it's on
COORD_DP = 5       # ~1 m; keeps the whole-city file under Vercel's ~50 MB/file limit


def q(**kw):
    for attempt in range(6):
        try:
            return soql_query(timeout=120, **kw)
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            wait = 3 * (2 ** attempt)
            print(f"    {type(e).__name__}: {e} -> retry in {wait}s")
            time.sleep(wait)


def q_geojson(dataset_id, where, limit=8000):
    url = f"https://data.cityofnewyork.us/resource/{dataset_id}.geojson"
    for attempt in range(6):
        try:
            r = requests.get(url, params={"$where": where, "$limit": limit}, timeout=120)
            r.raise_for_status()
            return r.json().get("features", [])
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            wait = 3 * (2 ** attempt)
            print(f"    {type(e).__name__}: {e} -> retry in {wait}s")
            time.sleep(wait)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def ring_area(ring):
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def simplify_ring(ring, tol_deg):
    """Cheap perpendicular-distance simplification. Footprints come with a
    vertex every ~0.3 m from photogrammetry; the map extrudes flat walls, so
    ~1 m of detail is invisible. Halves the vertex count on average."""
    if len(ring) <= 5:
        return ring
    out = [ring[0]]
    for i in range(1, len(ring) - 1):
        ax, ay = out[-1]
        bx, by = ring[i]
        cx, cy = ring[i + 1]
        dx, dy = cx - ax, cy - ay
        seg = (dx * dx + dy * dy) ** 0.5 or 1e-12
        # perpendicular distance of b from line a-c
        d = abs(dy * bx - dx * by + cx * ay - cy * ax) / seg
        if d > tol_deg:
            out.append(ring[i])
    out.append(ring[-1])
    return out if len(out) >= 4 else ring


# ---------------------------------------------------------------- step 1
buildings = json.load(open(DATA / "map_dataset.json"))
bids = sorted({b["buildingid"] for b in buildings})
print(f"step 1: {len(buildings)} buildings citywide, {len(bids)} distinct buildingids")

# ---------------------------------------------------------------- step 2
bid_cache = CACHE / "hpd_buildings.json"
seed = ROOT / "prototype" / "_cache" / "hpd_buildings.json"
if not bid_cache.exists() and seed.exists():
    bid_cache.write_text(seed.read_text())
    print(f"step 2: seeded from {seed} ({len(json.load(open(bid_cache)))} rows)")

bid_info = json.load(open(bid_cache)) if bid_cache.exists() else {}
todo = [b for b in bids if b not in bid_info]
if todo:
    batches = list(chunked(todo, 400))
    print(f"step 2: resolving buildingid -> bin/bbl for {len(todo)} new ids, {len(batches)} batches")
    for i, batch in enumerate(batches):
        where = "buildingid in(" + ",".join(f"'{b}'" for b in batch) + ")"
        rows = q(dataset_id=HPD_BUILDINGS, where=where,
                 select="buildingid,boroid,block,lot,bin", limit=5000)
        for r in rows:
            bid = r.get("buildingid")
            if not bid:
                continue
            boroid, block, lot = r.get("boroid"), r.get("block"), r.get("lot")
            bbl = None
            if boroid and block and lot:
                try:
                    bbl = f"{int(boroid)}{int(block):05d}{int(lot):04d}"
                except (ValueError, TypeError):
                    bbl = None
            bin_ = r.get("bin")
            if bin_ in ("", "0", None) or (isinstance(bin_, str) and bin_.endswith("000000")):
                bin_ = None
            prev = bid_info.get(bid)
            if prev and prev.get("bin") and not bin_:
                continue
            bid_info[bid] = {"bin": bin_, "bbl": bbl}
        if (i + 1) % 20 == 0 or i == len(batches) - 1:
            json.dump(bid_info, open(bid_cache, "w"))
            print(f"  step 2 batch {i+1}/{len(batches)}: {len(bid_info)} resolved")
    json.dump(bid_info, open(bid_cache, "w"))

bins_needed = sorted({v["bin"] for v in bid_info.values() if v.get("bin")})
bbls_needed = sorted({v["bbl"] for v in bid_info.values() if v.get("bbl")})
print(f"step 2 done: {len(bins_needed)} distinct BINs, {len(bbls_needed)} distinct BBLs")

# ---------------------------------------------------------------- step 3
ft_cache = CACHE / "footprints_raw.jsonl"
done_keys = set()
if ft_cache.exists():
    for line in open(ft_cache):
        try:
            done_keys.add(json.loads(line)["_k"])
        except Exception:  # noqa: BLE001
            pass
    print(f"step 3: {len(done_keys)} footprint batches already cached")

with open(ft_cache, "a") as fc:
    bin_batches = list(chunked(bins_needed, 400))
    for i, batch in enumerate(bin_batches):
        key = f"bin:{i}"
        if key in done_keys:
            continue
        where = "bin in(" + ",".join(f"'{b}'" for b in batch) + ")"
        feats = q_geojson(FOOTPRINTS, where, limit=8000)
        fc.write(json.dumps({"_k": key, "rows": feats}) + "\n")
        fc.flush()
        if (i + 1) % 20 == 0 or i == len(bin_batches) - 1:
            print(f"  step 3 BIN batch {i+1}/{len(bin_batches)}")
    bbl_batches = list(chunked(bbls_needed, 300))
    for i, batch in enumerate(bbl_batches):
        key = f"bbl:{i}"
        if key in done_keys:
            continue
        where = "base_bbl in(" + ",".join(f"'{b}'" for b in batch) + ")"
        feats = q_geojson(FOOTPRINTS, where, limit=12000)
        fc.write(json.dumps({"_k": key, "rows": feats}) + "\n")
        fc.flush()
        if (i + 1) % 20 == 0 or i == len(bbl_batches) - 1:
            print(f"  step 3 BBL batch {i+1}/{len(bbl_batches)}")

# ---------------------------------------------------------------- step 4
by_bin, by_bbl = {}, {}


def outer_rings(geom):
    if not geom:
        return []
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    polys = coords if t == "MultiPolygon" else [coords] if t == "Polygon" else []
    out = []
    for poly in polys:
        if not (poly and poly[0]):
            continue
        ring = [[round(x, COORD_DP), round(y, COORD_DP)] for x, y in poly[0]]
        dedup = [ring[0]]
        for p in ring[1:]:
            if p != dedup[-1]:
                dedup.append(p)
        if len(dedup) >= 4:
            out.append(simplify_ring(dedup, SIMPLIFY_M / 111320.0))
    return out


for line in open(ft_cache):
    rec = json.loads(line)
    for feat in rec["rows"]:
        r = feat.get("properties", {})
        rings = outer_rings(feat.get("geometry"))
        if not rings:
            continue
        try:
            h = float(r.get("height_roof") or 0) * FT_TO_M
        except (ValueError, TypeError):
            h = 0.0
        try:
            g = float(r.get("ground_elevation") or 0) * FT_TO_M
        except (ValueError, TypeError):
            g = 0.0
        area = sum(ring_area(x) for x in rings)
        entry = {"rings": rings, "height_m": h, "base_elev_m": g, "area": area}
        b = r.get("bin")
        if b and b not in ("", "0"):
            cur = by_bin.get(b)
            if not cur or area > cur["area"]:
                by_bin[b] = entry
        for f in ("base_bbl", "mappluto_bbl"):
            bb = r.get(f)
            if bb:
                cur = by_bbl.get(bb)
                if not cur or area > cur["area"]:
                    by_bbl[bb] = entry

out, n_bin, n_bbl, n_miss = [], 0, 0, 0
by_boro_miss = {}
for b in buildings:
    bid = b["buildingid"]
    info = bid_info.get(bid) or {}
    hit = src = None
    if info.get("bin") and info["bin"] in by_bin:
        hit, src = by_bin[info["bin"]], "bin"
        n_bin += 1
    elif info.get("bbl") and info["bbl"] in by_bbl:
        hit, src = by_bbl[info["bbl"]], "bbl"
        n_bbl += 1
    else:
        n_miss += 1
        by_boro_miss[b["boro"]] = by_boro_miss.get(b["boro"], 0) + 1
        continue
    fallback_h = float(b.get("floors") or 3) * 3.5
    out.append({
        "buildingid": bid,
        "height_m": round(hit["height_m"], 1) if hit["height_m"] > 3 else round(fallback_h, 1),
        "rings": hit["rings"],
    })

# compact separators + the trims above keep the citywide file well under
# Vercel's ~50 MB per-file limit (map.html hides the view toggle if it 404s).
json.dump(out, open(OUT, "w"), separators=(",", ":"))
sz = OUT.stat().st_size
verts = sum(len(r) for e in out for r in e["rings"])
print(f"\nstep 4 done -> {OUT}")
print(f"  matched {len(out)}/{len(buildings)}  (BIN {n_bin}, BBL {n_bbl}, unmatched {n_miss})")
print(f"  unmatched by borough: {by_boro_miss}")
print(f"  {verts} vertices, {sz/1e6:.1f} MB, avg {verts/max(len(out),1):.1f} verts/building")
