"""Verify src/building_story.js produces IDENTICAL output to src/building_story.py
on the same real buildings, before the JS version is trusted in the live map.

Runs the JS through a real headless browser (not node, to match how it'll
actually execute in production) and compares every field against the Python
version, for a deliberately varied set of real buildings (small/large,
isolated/persistent/chronic, dormant/active-surge) to exercise every branch.

Run: python scripts/verify_js_port.py
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from building_story import build_profile, generate_narrative  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
JS_PATH = Path(__file__).resolve().parent.parent / "src" / "building_story.js"
TODAY = datetime(2026, 8, 14)

# Deliberately varied real buildings from prior phases, spanning every
# pattern/recency/severity/engagement combination we've seen.
TEST_BUILDING_IDS = [
    "65175", "808705", "288857", "878031", "4172",       # large, chronic, mixed engagement
    "694446", "380289", "384515", "934498", "804699",    # minimal/low scale
    "222118", "155406",                                  # active surge, responsive
    "49694", "842723", "8623",                            # extreme severity / extreme backlog
    "9835", "133539", "8228", "285601", "300043",         # Widespread (9+ real defects, no recurring signature)
    "805595",                                              # Isolated with administrative filings mixed in
    "324815",                                              # Widespread, Gone-quiet/Unaddressed/Decades-overdue overlap
    "520214",                                              # Isolated, same overlap w/ non-compliance flag set
]


def load_violations_for(bids):
    """Pull raw records for these buildings from whichever cached raw file has them."""
    candidates = [
        DATA_DIR / "phase3_wide_sample_raw.json",
        DATA_DIR / "phase3_wider_sample_raw.json",
        DATA_DIR / "map_dataset_violations_raw.json",
    ]
    by_building = defaultdict(list)
    found = set()
    for path in candidates:
        if not path.exists():
            continue
        rows = json.load(open(path))
        for r in rows:
            if r["buildingid"] in bids:
                by_building[r["buildingid"]].append(r)
                found.add(r["buildingid"])
        if found >= set(bids):
            break
    return by_building


def compare(py_val, js_val, path, mismatches):
    if isinstance(py_val, float) and isinstance(js_val, (int, float)):
        if abs(py_val - js_val) > 0.01:
            mismatches.append(f"{path}: py={py_val!r} js={js_val!r}")
    elif py_val != js_val:
        mismatches.append(f"{path}: py={py_val!r} js={js_val!r}")


def main():
    by_building = load_violations_for(set(TEST_BUILDING_IDS))
    missing = [b for b in TEST_BUILDING_IDS if b not in by_building]
    if missing:
        print(f"WARNING: no cached raw data for {missing}, skipping those")

    js_source = JS_PATH.read_text(encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(content=js_source)

        total_checked = 0
        total_mismatches = 0

        for bid in TEST_BUILDING_IDS:
            if bid not in by_building:
                continue
            viols = by_building[bid]
            py_profile = build_profile(bid, viols, TODAY)
            py_narrative = generate_narrative(py_profile)
            py_dict = {k: v for k, v in py_profile.__dict__.items()}

            js_result = page.evaluate(
                """([viols, todayIso]) => {
                    const today = new Date(todayIso);
                    const p = buildProfile(p_bid, viols, today);
                    return { profile: p, narrative: generateNarrative(p) };
                }""".replace("p_bid", json.dumps(bid)),
                [viols, TODAY.isoformat() + "Z"],
            )
            js_profile = js_result["profile"]
            js_narrative = js_result["narrative"]

            mismatches = []
            for key, py_val in py_dict.items():
                js_val = js_profile.get(key)
                compare(py_val, js_val, f"{bid}.{key}", mismatches)
            compare(py_narrative, js_narrative, f"{bid}.narrative", mismatches)

            total_checked += 1
            status = "MATCH" if not mismatches else "MISMATCH"
            print(f"[{status}] {bid} ({py_profile.address}, {len(viols)} raw records)")
            if mismatches:
                total_mismatches += len(mismatches)
                for m in mismatches:
                    print(f"    {m}")
                print(f"    PY narrative: {py_narrative}")
                print(f"    JS narrative: {js_narrative}")

        browser.close()

    print(f"\n{'='*60}")
    print(f"Checked {total_checked} buildings, {total_mismatches} field mismatches")
    sys.exit(1 if total_mismatches else 0)


if __name__ == "__main__":
    main()
