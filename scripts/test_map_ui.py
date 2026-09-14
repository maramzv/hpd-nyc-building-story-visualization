"""Automated browser test of map.html against the 8 user flows.

Uses a real headless Chromium via Playwright - actually loads the page,
clicks things, and checks the resulting DOM state and console for errors,
rather than just reading the source code.

Run: python scripts/test_map_ui.py
Requires the local dev server running (python scripts/dev_server.py) and
Playwright/Chromium installed.

NOTE: this suite predates the landing-screen redesign - it looks for
`#landing-stats .stat` / `.num`, which are now `.lp-stat` / `.lp-num`, so
Flow 1 fails for that reason alone. Left as-is rather than silently
rewritten; scripts/test_router.py and scripts/test_web_data.py cover the
routing and data layers in the meantime.
"""
import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8232/map"
failures = []
console_errors = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"UNCAUGHT: {exc}"))

        print("Flow 1: Load page, landing overlay appears with real stats")
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_selector("#landing-stats .stat", timeout=15000)
        stat_texts = page.locator("#landing-stats .num").all_inner_texts()
        check("landing overlay visible", page.locator("#landing").is_visible())
        check("5 stat percentages populated", len(stat_texts) == 5, str(stat_texts))
        check("stats are real percentages", all("%" in t for t in stat_texts), str(stat_texts))
        check("legend rows present", page.locator("#landing-legend .sw").count() == 5)

        print("\nFlow 2: Click Explore, landing disappears")
        page.click("#landing-enter")
        page.wait_for_timeout(500)
        check("landing hidden after click", not page.locator("#landing").is_visible())

        print("\nFlow 3-4: Borough click drills into its neighborhoods view; neighborhood click doesn't error")
        # Timeout bumped from 10000: at full scale (167k buildings) the initial
        # deck.gl WebGL layer construction competes for the main thread and can
        # delay Playwright's own visibility polling even after the element is
        # actually visible.
        page.wait_for_selector(".boro-row", timeout=25000)
        first_boro = page.locator(".boro-row").first
        first_boro.click()
        page.wait_for_timeout(300)
        check("drilled into neighborhoods view",
              "block" == page.evaluate("document.getElementById('neighborhoods-view').style.display"))
        nbhd_rows = page.locator("#neighborhoods-list .nbhd-row")
        check("neighborhoods rendered in drill-down", nbhd_rows.count() > 0, f"count={nbhd_rows.count()}")
        if nbhd_rows.count() > 0:
            nbhd_rows.first.click()
            page.wait_for_timeout(300)
        page.click("#all-boroughs-back")
        page.wait_for_timeout(500)

        print("\nFlow 5-6: Click a building - triggers a REAL live fetch to NYC's API, "
              "computes the story client-side, and shows evidence + similar-buildings")
        test_data = page.evaluate("window.__TEST_DATA__ ? window.__TEST_DATA__.length : 0")
        check("test data hook populated", test_data > 0, f"len={test_data}")
        # Find a building with active_count > 1 so the evidence table has something to show
        sample = page.evaluate("""
            () => {
                const withEvidence = window.__TEST_DATA__.find(d => d.active_count > 3);
                showDetail(withEvidence);
                return withEvidence;
            }
        """)
        check("detail panel visible immediately (batch data)", page.locator("#detail").is_visible())
        check("shows loading state while live fetch is in flight",
              page.locator("#d-narrative .skel").count() > 0)
        # Wait for the REAL network round-trip to NYC's API to resolve (not a fixed sleep -
        # poll until the placeholder text is gone, with a generous timeout since NYC's API
        # has been observed to be occasionally slow)
        page.wait_for_function(
            "() => !document.getElementById('d-narrative').textContent.includes('Loading')",
            timeout=15000,
        )
        headline = page.locator("#d-headline").inner_text()
        check("headline matches pattern (live-confirmed)", sample["pattern"] in headline, headline)
        narrative = page.locator("#d-narrative").inner_text()
        check("live narrative populated", len(narrative) > 10 and "failed" not in narrative.lower(), narrative)
        ev_count_text = page.locator("#evidence-count").inner_text()
        check("live evidence count shown", "record" in ev_count_text, ev_count_text)

        similar_text = page.locator("#d-similar").inner_text()
        check("similar-buildings text populated", len(similar_text) > 5, similar_text)

        # Story / Evidence / Timeline tabs (replaced the old evidence-toggle)
        page.click('#story-tabs button[data-tab="evidence"]')
        page.wait_for_timeout(200)
        check("evidence tab activates", page.locator("#tab-evidence").evaluate("el => el.classList.contains('active')"))
        ev_rows = page.locator("#tab-evidence .ev-row").count()
        check("evidence rows rendered", ev_rows > 0, f"rows={ev_rows}")

        page.click('#story-tabs button[data-tab="timeline"]')
        page.wait_for_timeout(200)
        check("timeline tab activates", page.locator("#tab-timeline").evaluate("el => el.classList.contains('active')"))
        page.wait_for_function(
            "() => document.querySelectorAll('#timeline-body .tl-item').length > 0 "
            "|| document.getElementById('timeline-empty')",
            timeout=5000,
        )
        tl_items = page.locator("#timeline-body .tl-item").count()
        check("timeline entries rendered", tl_items > 0, f"items={tl_items}")
        if tl_items > 0:
            page.locator("#timeline-body .tl-head").first.click()
            page.wait_for_timeout(200)
            check("timeline entry expands on click",
                  page.locator("#timeline-body .tl-item.open").count() > 0)

        page.click('#story-tabs button[data-tab="story"]')
        page.wait_for_timeout(150)

        print("\nFlow 7: REAL mouse hover over a rendered building shows the tooltip with correct content")
        hover_target = page.evaluate("""
            () => {
                const t = window.__TEST_DATA__.find(d => d.active_count > 10 && d.active_count < 30);
                const map = window.__TEST_MAP__;
                map.setPitch(0); map.setBearing(0);
                map.jumpTo({ center: [t.lon, t.lat], zoom: 19 });
                return t;
            }
        """)
        page.wait_for_timeout(800)  # extra margin at 167k-building scale for the layer to re-render
        box = page.locator("#map").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(400)
        check("tooltip visible on real hover", page.locator("#tooltip").is_visible())
        tooltip_text = page.locator("#tooltip").inner_text()
        check("tooltip shows the hovered building's own address",
              hover_target["address"] in tooltip_text, f"got: {tooltip_text}")
        # Move away (realistic gradual movement, not an instant jump) and confirm it hides
        far_x, far_y = box["x"] + box["width"] - 20, box["y"] + box["height"] - 20
        page.mouse.move(far_x, far_y, steps=10)
        page.wait_for_timeout(800)
        check("tooltip hides when mouse moves off a building", not page.locator("#tooltip").is_visible())

        print("\nFlow 8: Search box returns results and clicking one opens detail")
        # The search box lives in the Explorer view, which Flow 5-6 slid away
        # from when it opened a building's Story view - go back first.
        page.click("#back-button")
        page.wait_for_timeout(450)  # matches map.html's SLIDE_MS (320) plus margin
        # Use a real address fragment from the loaded sample
        search_term = page.evaluate("window.__TEST_DATA__[5].address.split(' ').slice(1,3).join(' ')")
        page.fill("#search-box", search_term)
        page.wait_for_timeout(300)
        results_open = page.locator("#search-results").evaluate("el => el.classList.contains('open')")
        check(f"search results open for '{search_term}'", results_open)
        result_count = page.locator(".search-result").count()
        check("at least one search result", result_count > 0, f"count={result_count}")
        if result_count > 0:
            page.locator(".search-result").first.click()
            page.wait_for_timeout(300)
            check("detail panel still visible after search click", page.locator("#detail").is_visible())

        print("\nFlow 9: REAL pixel click on the rendered 3D column (not a direct function call) - "
              "tests deck.gl's actual click-picking pipeline end to end")
        target = page.evaluate("""
            () => {
                // pick a distinctive building so we can prove the click hit THIS one
                const t = window.__TEST_DATA__.find(d => d.active_count > 10 && d.active_count < 30);
                const map = window.__TEST_MAP__;
                map.setPitch(0); map.setBearing(0); // top-down, so screen position == geographic position, no 3D perspective offset
                map.jumpTo({ center: [t.lon, t.lat], zoom: 19 }); // jumpTo = instant, no animation to wait out
                return t;
            }
        """)
        page.wait_for_timeout(800)  # let the layer re-render at the new viewport (167k buildings)
        box = page.locator("#map").bounding_box()
        center_x, center_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.click(center_x, center_y)
        page.wait_for_timeout(300)
        clicked_id = page.locator("#d-buildingid").inner_text()
        check(f"real canvas click picked the correct building ({target['address']})",
              target["buildingid"] in clicked_id, f"got: {clicked_id}")

        print(f"\n=== Console errors during entire run: {len(console_errors)} ===")
        for e in console_errors:
            print("  ERROR:", e)

        browser.close()

        print(f"\n{'='*50}")
        print(f"RESULT: {len(failures)} failing checks, {len(console_errors)} console errors")
        if failures:
            print("Failed checks:", failures)
        sys.exit(1 if (failures or console_errors) else 0)


if __name__ == "__main__":
    main()
