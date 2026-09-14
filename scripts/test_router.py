"""Verifies the client-side router: five real URLs, back/forward, per-view
titles, legacy redirects, and that the info views keep their own CSS cascade.

Start the dev server first:  python scripts/dev_server.py
Then:                        python scripts/test_router.py
"""
import os
import sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SITE_BASE", "http://127.0.0.1:8232")
fails, console = [], []

TITLES = {
    "/": "NYC Building Stories",
    "/map": "NYC Building Stories - Map",
    "/about": "NYC Building Stories - About",
    "/data": "NYC Building Stories - Data",
    "/methodology": "NYC Building Stories - Methodology",
}


def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def path_of(page):
    return urlparse(page.url).path.rstrip("/") or "/"


def view_of(page):
    return page.evaluate("""(() => {
        const b = document.body.classList;
        if (b.contains('info-open')) return 'info';
        if (b.contains('in-map')) return 'map';
        return 'home';
    })()""")


def main():
    with sync_playwright() as p:
        br = p.chromium.launch()
        page = br.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console.append(f"UNCAUGHT: {e}"))
        # The shell pulls ~44MB of borough slices on every load; the little
        # local dev server is still streaming the previous page's when the
        # next navigation starts.
        page.set_default_navigation_timeout(180000)
        page.set_default_timeout(60000)
        # Router behaviour doesn't depend on footprint geometry, and skipping
        # it takes ~30MB off every one of the ~20 page loads below. The data
        # layer's own suite covers footprints in full.
        page.route("**/footprints-*.json", lambda r: r.abort())

        print("1. All 5 URLs work typed fresh into the browser")
        nav_boxes = {}
        for path, title in TITLES.items():
            page.goto(BASE + path, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            check(f"{path} title", page.title() == title, f"got {page.title()!r}")
            expect = {"/": "home", "/map": "map"}.get(path, "info")
            check(f"{path} shows the {expect} view", view_of(page) == expect, f"got {view_of(page)}")
            desc = page.evaluate("document.querySelector('meta[name=description]').content")
            check(f"{path} has a meta description", bool(desc) and len(desc) > 30)
            nav_boxes[path] = page.evaluate("""(() => {
                const r = el => { const b = el.getBoundingClientRect();
                    return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
                return {
                    nav: r(document.getElementById('site-nav')),
                    brand: r(document.querySelector('#site-nav .lp-brand')),
                    right: r(document.querySelector('#site-nav .lp-nav-right')),
                };
            })()""")

        print("2. Info views render their own content and their own cascade")
        page.goto(BASE + "/about", wait_until="domcontentloaded")
        page.wait_for_selector("#info-view .content", timeout=15000)
        check("about content present", "Evidence, not" in page.inner_text("#info-view h1"),
              page.inner_text("#info-view h1"))
        about_lede = page.evaluate(
            "getComputedStyle(document.querySelector('#info-view p.lede')).fontSize")
        check("about p.lede is 16.5px", about_lede == "16.5px", about_lede)
        page.goto(BASE + "/data", wait_until="domcontentloaded")
        page.wait_for_selector("#info-view .content", timeout=15000)
        data_lede = page.evaluate(
            "getComputedStyle(document.querySelector('#info-view p.lede')).maxWidth")
        # data.html scoped its lede to 680px; about.html did not. Proves the
        # per-view stylesheets are not bleeding into each other.
        check("data p.lede keeps its own max-width", data_lede == "680px", data_lede)

        print("3. Nav is pixel-identical across all five views")
        ref = nav_boxes["/"]
        for path, box in nav_boxes.items():
            for part in ("nav", "brand", "right"):
                check(f"{path} nav '{part}' matches home", box[part] == ref[part],
                      f"{box[part]} vs {ref[part]}")

        print("4. In-app navigation does not reload the document")
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        page.evaluate("window.__NO_RELOAD__ = true")
        page.click("#site-nav .lp-nav-links a[href='/about']")
        page.wait_for_timeout(900)
        check("still same document after nav click", page.evaluate("window.__NO_RELOAD__ === true"))
        check("URL updated to /about", page.url.endswith("/about"), page.url)
        check("title updated", page.title() == TITLES["/about"], page.title())
        check("nav link marked active",
              page.evaluate("document.querySelector(\"#site-nav .lp-nav-links a[href='/about']\")"
                            ".classList.contains('active')"))

        print("5. Back / forward behave correctly")
        page.click("#site-nav .lp-nav-links a[href='/methodology']")
        page.wait_for_timeout(900)
        check("now on /methodology", page.url.endswith("/methodology"), page.url)
        page.go_back()
        page.wait_for_timeout(900)
        check("back -> /about", page.url.endswith("/about"), page.url)
        check("back restored about title", page.title() == TITLES["/about"], page.title())
        check("back restored about content", "Evidence, not" in page.inner_text("#info-view h1"))
        page.go_back()
        page.wait_for_timeout(900)
        check("back -> home", path_of(page) == "/", page.url)
        check("home view restored", view_of(page) == "home", view_of(page))
        page.go_forward()
        page.wait_for_timeout(900)
        check("forward -> /about", page.url.endswith("/about"), page.url)
        check("still no document reload", page.evaluate("window.__NO_RELOAD__ === true"))

        print("6. No CSS leak from info views back onto Home")
        page.goto(BASE + "/about", wait_until="domcontentloaded")
        page.wait_for_selector("#info-view .content", timeout=15000)
        page.wait_for_timeout(500)
        page.evaluate("window.__NO_RELOAD__ = true")
        page.click("#brand-home-btn")
        page.wait_for_timeout(900)
        check("logo returns to home", view_of(page) == "home", view_of(page))
        check("URL is /", path_of(page) == "/", page.url)
        check("info stylesheet torn down",
              page.evaluate("document.getElementById('info-view').innerHTML === ''"))
        landing_main = page.evaluate(
            "getComputedStyle(document.querySelector('#landing .lp-main')).justifyContent")
        # about.css sets `main { justify-content: center }`; the landing's own
        # rule does not. If the sheet leaked, this would read 'center'.
        check("landing <main> keeps its own styling", landing_main != "center", landing_main)

        print("7. Explore the map still goes to /map, instantly")
        page.wait_for_function("window.__TEST_DATA__ && window.__TEST_DATA__.length > 0", timeout=120000)
        page.evaluate("window.__NO_RELOAD__ = true")
        page.click("#landing-enter")
        page.wait_for_timeout(1500)
        check("entered map view", view_of(page) == "map", view_of(page))
        check("URL is /map", page.url.endswith("/map"), page.url)
        check("map title", page.title() == TITLES["/map"], page.title())
        check("no reload on explore", page.evaluate("window.__NO_RELOAD__ === true"))
        page.go_back()
        page.wait_for_timeout(900)
        check("back from map -> home", view_of(page) == "home", view_of(page))

        print("8. Legacy URLs redirect instead of 404ing")
        for old, expect_suffix in [("/about.html", "/about"), ("/data.html", "/data"),
                                   ("/methodology.html", "/methodology"),
                                   ("/map.html", ""), ("/map.html?explore=1", "/map")]:
            page.goto(BASE + old, wait_until="domcontentloaded")
            page.wait_for_timeout(700)
            ok = path_of(page) == (expect_suffix or "/")
            check(f"{old} -> {expect_suffix or '/'}", ok, page.url)

        print("9. Direct load of /methodology is fully rendered (refresh-safe)")
        page.goto(BASE + "/methodology", wait_until="domcontentloaded")
        page.wait_for_selector("#info-view .content", timeout=15000)
        check("methodology heading present", len(page.inner_text("#info-view h1")) > 0)
        check("methodology is long-form", len(page.inner_text("#info-view")) > 5000,
              str(len(page.inner_text("#info-view"))))
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#info-view .content", timeout=15000)
        check("survives a refresh", page.title() == TITLES["/methodology"], page.title())

        print("10. Explore the map works from an info view too")
        page.goto(BASE + "/about", wait_until="domcontentloaded")
        page.wait_for_selector("#info-view .content", timeout=15000)
        # The button is hidden on the map view but must stay visible here,
        # exactly as it was on the standalone info pages.
        check("explore button visible on info view",
              page.locator("#landing-enter").is_visible())
        page.wait_for_function("window.__TEST_DATA__ && window.__TEST_DATA__.length > 0",
                               timeout=120000)
        page.evaluate("window.__NO_RELOAD__ = true")
        page.click("#landing-enter")
        page.wait_for_timeout(1500)
        check("info view -> map", view_of(page) == "map", view_of(page))
        check("URL is /map", path_of(page) == "/map", page.url)
        check("info markup torn down",
              page.evaluate("document.getElementById('info-view').innerHTML === ''"))
        check("no reload", page.evaluate("window.__NO_RELOAD__ === true"))
        page.go_back()
        page.wait_for_timeout(1000)
        check("back returns to /about", path_of(page) == "/about", page.url)
        check("about content restored", view_of(page) == "info", view_of(page))

        # ERR_FAILED entries are the footprint requests this suite aborts on
        # purpose (see page.route above), not real page errors.
        real = [e for e in console
                if "favicon" not in e.lower() and "ERR_FAILED" not in e]
        check("no console errors", not real, "; ".join(real[:3]))
        br.close()

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        sys.exit(1)
    print("ALL ROUTER CHECKS PASSED")


main()
