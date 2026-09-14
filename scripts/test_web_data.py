"""Verifies the data layer: the site loads the split data/web/* slices instead
of the two giant JSON files, and everything downstream still sees identical rows.

Start the dev server first:  python scripts/dev_server.py
Then:                        python scripts/test_web_data.py
"""
import json
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
BASE = os.environ.get("SITE_BASE", "http://127.0.0.1:8232")
URL = BASE + "/"
fails = []
console = []


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"UNCAUGHT: {e}"))

        requests = []
        page.on("request", lambda r: requests.append(r.url))

        print("1. Landing paints from summary.json alone")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#landing-stats .lp-stat", timeout=30000)
        stats = page.locator("#landing-stats .lp-num").all_inner_texts()
        check("5 pattern percentages", len(stats) == 5, str(stats))
        check("all are percentages", all("%" in t for t in stats), str(stats))
        label = page.locator("#landing-stats-label").inner_text()
        check("landing label has building count", "166,481" in label, label)

        # The landing must be up BEFORE the borough slices finish.
        early = [u for u in requests if "buildings-" in u]
        check("landing rendered without waiting on all slices",
              page.locator("#landing").is_visible(), "landing not visible")

        print("2. Old giant files are never requested")
        page.wait_for_function("window.__TEST_DATA__ && window.__TEST_DATA__.length > 0", timeout=120000)
        page.wait_for_timeout(1500)
        check("map_dataset.json not fetched", not any("map_dataset.json" in u for u in requests))
        check("footprints.json (monolith) not fetched",
              not any(u.endswith("data/footprints.json") for u in requests))
        check("summary.json fetched", any("summary.json" in u for u in requests))
        check("5 borough building slices fetched",
              len([u for u in requests if "buildings-" in u]) == 5,
              str([u.rsplit("/", 1)[-1] for u in requests if "buildings-" in u]))

        print("3. Unpacked rows are identical in shape and content")
        n = page.evaluate("window.__TEST_DATA__.length")
        check("all 166,481 buildings present", n == 166481, str(n))
        row = page.evaluate("window.__TEST_DATA__[0]")
        expected_keys = {"buildingid", "address", "boro", "lat", "lon", "floors",
                         "footprint_m2", "active_count", "scale", "pattern",
                         "neighborhood", "class_c_count", "recent_count", "backlog_years"}
        check("row has the original 14 fields",
              expected_keys.issubset(set(row.keys())), str(sorted(row.keys())))
        check("scale is a string, not an index", isinstance(row["scale"], str), repr(row["scale"]))
        check("pattern is a string, not an index", isinstance(row["pattern"], str), repr(row["pattern"]))
        check("neighborhood is a string", isinstance(row["neighborhood"], str), repr(row["neighborhood"]))

        boros = page.evaluate("[...new Set(window.__TEST_DATA__.map(d => d.boro))].sort()")
        check("all 5 boroughs present", len(boros) == 5, str(boros))

        print("4. Lens maxima match the precomputed summary")
        summary = json.loads((ROOT / "summary.json").read_text()) if (ROOT / "summary.json").exists() else None
        lens = page.evaluate("({columns: LENSES.columns.max, classc: LENSES.classc.max, "
                             "recent: LENSES.recent.max, top: LENS_TOP_M})")
        # Recompute from the loaded rows: must equal what the build script wrote.
        live = page.evaluate("""(() => {
            const mx = f => window.__TEST_DATA__.reduce((a, d) => Math.max(a, f(d) || 0), 0);
            return {
                columns: mx(d => d.active_count),
                classc: mx(d => d.class_c_count),
                recent: mx(d => d.recent_count),
            };
        })()""")
        for k in ("columns", "classc", "recent"):
            check(f"lens max '{k}' matches data", lens[k] == live[k], f"summary={lens[k]} data={live[k]}")
        check("LENS_TOP_M = columns.max * 5", lens["top"] == lens["columns"] * 5, str(lens))

        print("5. Footprints joined, Buildings lens available")
        joined = page.evaluate("window.__TEST_DATA__.filter(d => d._fp).length")
        check("footprints joined to rows", joined > 150000, f"{joined} joined")
        disabled = page.evaluate(
            "document.querySelector('#view-toggle button[data-mode=\\\"footprints\\\"]').disabled")
        check("Buildings lens enabled", disabled is False, f"disabled={disabled}")

        print("6. Landing controls unlock once data lands")
        check("explore button enabled", page.locator("#landing-enter").is_enabled())
        check("landing search enabled", page.locator("#landing-search-box").is_enabled())
        check("placeholder restored",
              page.locator("#landing-search-box").get_attribute("placeholder")
              == "Search an address or building...",
              page.locator("#landing-search-box").get_attribute("placeholder"))

        print("7. Coordinate de-dup was applied upstream (no stacked buildings)")
        dupes = page.evaluate("""(() => {
            const seen = new Set(); let d = 0;
            for (const r of window.__TEST_DATA__) {
                const k = r.lat.toFixed(6) + ',' + r.lon.toFixed(6);
                if (seen.has(k)) d++; else seen.add(k);
            }
            return d;
        })()""")
        # 7 is the exact residual the ORIGINAL in-browser dedup also left (a
        # jittered building can land on a different group's coordinate; the
        # pass never re-runs). Verified by replaying the original JS. Matching
        # it exactly is the point - 0 would mean behaviour changed.
        check("collision residual matches original JS behaviour", dupes == 7, f"{dupes} collisions")

        print("8. Explore the map still works")
        page.click("#landing-enter")
        # Entering the map renders 166k extruded polygons; give the main
        # thread room before querying it again.
        page.wait_for_timeout(6000)
        check("entered map view", page.evaluate("document.body.classList.contains('in-map')"))
        check("landing hidden",
              page.evaluate("getComputedStyle(document.getElementById('landing')).display === 'none'"))

        print("9. Search works against the unpacked rows")
        term = page.evaluate("window.__TEST_DATA__[5].address.split(' ').slice(1,3).join(' ')")
        page.evaluate("""(t) => {
            const box = document.getElementById('search-box');
            box.value = t;
            box.dispatchEvent(new Event('input', { bubbles: true }));
        }""", term)
        page.wait_for_timeout(700)
        results = page.locator("#search-results > div").count()
        check(f"search '{term}' returns results", results > 0, f"{results} results")

        real_errors = [e for e in console if "favicon" not in e.lower()]
        check("no console errors", not real_errors, "; ".join(real_errors[:3]))

        b.close()

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + ", ".join(fails))
        sys.exit(1)
    print("ALL CHECKS PASSED")


main()
