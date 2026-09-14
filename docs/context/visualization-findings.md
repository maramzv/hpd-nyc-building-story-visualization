# Visualization Findings

The final write-up for this project: what the data shows, what the visualization reveals, and what it doesn't.

## The question

> What is happening to NYC's buildings, and what patterns are visible in the city's housing violation records?

## What was built

A live pipeline from NYC's Open HPD Violations dataset (`csn4-vhvf`) to an interactive 3D map (`index.html`, served at `/map`):

**Records → Building profiles → Story → Map.** Raw violation records are grouped by building, reduced to six evidence-based dimensions (Scale, Recency, Severity, Engagement, Pattern, Backlog age — see `docs/context/story-taxonomy-original-design.md`) plus one independent flag (Long-unresolved), joined against NYC's PLUTO dataset for real coordinates and floor counts, and rendered as 3D columns on a real map — colored by Pattern, height-scaled to each building's actual floor count, clickable for the full evidence-based narrative and the underlying violation records.

**Coverage:** the **full citywide population — 167,121 buildings**, every building with at least one currently-open HPD violation, not a sample. (PLUTO matched 167,265 of 167,684 buildings with a valid borough/block/lot, 99.75%; neighborhood matching via NYC's NTA boundaries covered 167,110 of 167,121, essentially 100%, across 198 distinct neighborhoods.)

## How the Pattern buckets are defined

The map colors each building by **Pattern**, which describes recurrence and volume of *real* (non-administrative) defects — not severity, and not landlord behavior:

| Pattern | Meaning |
|---|---|
| **No real defects** | Every violation on file is administrative (annual bedbug-report filing, registration lapse, signage) — no physical defect ever cited |
| **Isolated** | At least one real defect, but no defect has recurred and the total count is typical (below the citywide 75th percentile for non-recurring buildings, = 9) |
| **Widespread** | 9+ distinct real defects, none of which ever recurred — many different one-off problems rather than one repeating one |
| **Persistent** | The same defect (same order code, coherent description, any apartment) has recurred ≥3 times over ≥2 years |
| **Chronic** | The same defect has recurred ≥10 times over ≥5 years |

Recurrence is measured **building-wide** (a defect repeating across different apartments counts), which is the single largest correction made since the earlier sample-based version — see "What changed" below.

Separately, **Long-unresolved** is a yes/no flag, independent of Pattern: true when a building has *never* had a certification accepted or rejected, has had no activity in the past year, and has at least one violation 9.7+ years past its correction deadline. A Chronic building can also be Long-unresolved; so can an Isolated one.

## What the data shows

**Citywide Pattern distribution (all 167,121 buildings):**

| Pattern | Count | % |
|---|---|---|
| No real defects | 33,020 | 19.8% |
| Isolated | 76,708 | 45.9% |
| Widespread | 29,438 | 17.6% |
| Persistent | 23,465 | 14.0% |
| Chronic | 4,490 | 2.7% |

Roughly **one in five** buildings with an "open violation" has nothing physical on file at all — only paperwork. Among the ~134,000 buildings with a real defect, most (57%) show a single isolated problem; about 22% show many unrelated problems (Widespread); and about **21% show a genuine recurring defect** (Persistent + Chronic).

**40.3% of all buildings (67,285) carry the Long-unresolved flag** — no engagement, no recent activity, and a violation nearly a decade or more past deadline. This is not the same as "40% are actively dangerous today"; it means a large share of the open backlog is old and untouched on both sides of the process (see Limitations).

**Scale (raw open-violation count) is heavily concentrated:** median 6, 90th percentile 39, 99th percentile 159, maximum 1,262. 36% of buildings have 3 or fewer open violations; 4.9% ("Severe") have 67 or more.

**By borough — recurrence is very much not uniform:**

| Borough | Buildings | No real defects | Isolated | Widespread | Persistent | Chronic | Persistent + Chronic |
|---|---|---|---|---|---|---|---|
| Bronx | 25,121 | 18.0% | 37.7% | 17.9% | 20.7% | 5.7% | **26.4%** |
| Manhattan | 21,070 | 9.4% | 45.8% | 18.5% | 21.8% | 4.6% | **26.3%** |
| Brooklyn | 71,943 | 20.4% | 45.7% | 18.6% | 13.2% | 2.1% | 15.3% |
| Staten Island | 6,133 | 23.8% | 49.3% | 17.0% | 8.7% | 1.2% | 9.9% |
| Queens | 42,854 | 24.2% | 50.7% | 15.4% | 8.6% | 1.2% | 9.7% |

The Bronx and Manhattan show recurring-defect rates (Persistent + Chronic ≈ 26%) roughly **2.7x** those of Queens and Staten Island (≈ 10%). Manhattan also has by far the lowest "No real defects" share (9.4% vs. 18–24% elsewhere) — when a Manhattan building is in this dataset, it is much more likely to have a real physical problem on file. These are observed patterns in the full population, not estimates and not causal claims.

**At the neighborhood level the concentration sharpens well beyond the borough average.** Neighborhoods with the highest combined Persistent + Chronic rate (among the 150+ with at least 100 buildings):

| Neighborhood | Borough | Buildings | Persistent + Chronic |
|---|---|---|---|
| Fordham Heights | Bronx | 365 | 58.9% |
| Inwood | Manhattan | 321 | 57.0% |
| Washington Heights (North) | Manhattan | 655 | 56.6% |
| Manhattanville–West Harlem | Manhattan | 264 | 55.3% |
| Washington Heights (South) | Manhattan | 868 | 51.0% |
| Norwood | Bronx | 506 | 46.6% |
| University Heights (North)–Fordham | Bronx | 523 | 46.3% |
| Concourse–Concourse Village | Bronx | 773 | 43.5% |
| Bedford Park | Bronx | 743 | 42.4% |
| Highbridge | Bronx | 472 | 42.2% |
| Hamilton Heights–Sugar Hill | Manhattan | 1,044 | 41.7% |

In Fordham Heights, nearly **3 in 5** buildings with an open violation have a recurring defect — against a Bronx-wide rate of 26%. The recurring-defect problem is not spread evenly across the Bronx or upper Manhattan; it is concentrated in a compact set of northern-Manhattan / west-Bronx neighborhoods. This is exactly the pattern a borough-level view smooths over.

## What this means, and what it doesn't

Following this project's wording rules throughout (`docs/context/story-taxonomy-original-design.md`): these are direct, derived facts about the buildings' documented open records — not claims about landlords, safety, or livability, and not predictions.

- **"Chronic" does not mean "worst."** It means one defect signature recurred at high volume over a long span. A Chronic building can still show responsive certification behavior, and an Isolated building can have a single very severe (Class C) problem — severity is tracked as its own dimension, deliberately not folded into Pattern.
- **"Isolated" and "No real defects" are neutral findings, not a clean bill of health.** Closed and dismissed violations are excluded from this dataset entirely (confirmed via the API's own server-side filter — see `docs/context/data-discovery.md`), so "no recurring defect found" describes this open feed, not the building's full history.
- **"Widespread" is about count, not danger.** It flags a building generating many different one-off defects — a distinct story from one recurring problem, and one the earlier three-bucket scheme could not tell.
- **The borough and neighborhood differences are descriptive, not causal.** Higher recurring-defect rates in upper Manhattan and the west Bronx describe what is on file; this project makes no claim about why (building age, ownership concentration, enforcement patterns, and reporting rates are all outside this dataset's scope).

## What changed since the earlier (sample-based) write-up

The previous version of this document reported a 2,983-building random sample and three Pattern buckets (Isolated / Persistent / Chronic), with a citywide split of 90.8% / 8.7% / 0.5%. Three things changed:

1. **Full population, not a sample.** All 167,121 buildings are now profiled and mapped.
2. **Two new Pattern buckets.** "No real defects" (~20% of buildings, previously hidden inside Isolated) and "Widespread" (~18%, also previously inside Isolated) were split out after the 2026-08-16 taxonomy investigation found Isolated was making a "one small thing happened" claim for buildings with 35+ unrelated defects or zero real defects at all.
3. **Building-wide recurrence.** Recurrence was previously measured only *within the same apartment*, which structurally missed a defect (e.g. a mice infestation) recurring across many different units. Measuring it building-wide moved ~15,000 buildings up a bucket and roughly tripled the Chronic count. Full detail: `docs/logs/2026-08-16-taxonomy-investigation-full-findings.md`.

The headline effect: the old "90.8% Isolated" was overstated. Once paperwork-only and high-volume-one-off buildings are separated out and recurrence is measured properly, only about **46%** of buildings are genuinely "one isolated real problem," and the recurring-defect share rose from ~9% to ~17%.

## Limitations

- **Dataset scope: currently-open violations only.** Every finding describes the active backlog, not the full history of every violation ever issued. Closed and dismissed violations are filtered out server-side.
- **"Open" overstates "live."** The 2026-08-16 investigation found that ~43% of open, never-certified violations have had no recorded status change in 5+ years, and certified-fixed violations can sit in this feed for a decade or more. Backlog-age math now excludes violations the owner already certified as fixed, but "open in the system" still does not always mean "an active, unresolved condition today."
- **Recurring signatures can conflate a real repeating problem with a generic repair code.** Order code 502 ("repair with similar material...") covered 20 different structural defects at one building. The narrative wording distinguishes these (it says "the same administrative code — covering several different underlying defects" when a signature's descriptions don't cohere), but the Persistent/Chronic *bucket counts* themselves do not yet require description coherence.
- **Administrative-filing exclusion is not exhaustive.** Only the top 60 of 396 `OrderNumber` codes were manually audited; the remaining 336 (a small share of records by volume) have not been individually checked.
- **PLUTO join:** ~0.25% of buildings could not be matched to coordinates (mostly condo/complex sub-lots) and are excluded from the map, not misplaced. PLUTO also gives one coordinate per tax lot, so multiple distinct HPD BuildingIDs on the same lot are spread apart client-side for display and click-picking rather than truly geolocated.
- **Class "I" notices** carry no correction deadline and are info-only; they are counted toward Scale but not toward Backlog age.

## How to view it

```bash
python -m http.server 8000
```
Then open `http://localhost:8000/` in a browser. (The site is a client-side-routed
single page: `/`, `/map`, `/about`, `/data` and `/methodology` are all real URLs,
rewritten to `index.html` by `vercel.json`. A plain static server has no rewrites,
so only `/` works locally unless you serve the rewrites yourself.)

Requires the built web data in `data/web/` (generated by `scripts/build_web_data.py`
from `data/map_dataset.json` + `data/footprints.json`, and git-tracked for deploy).
Those two source files are intermediates and are deliberately *not* tracked - they are
55MB and 36MB, past GitHub's large-file warning. Regenerate them with the `scripts/`
pipeline, then run `build_web_data.py` to produce what the site actually loads.
A live deployment is served from Vercel.

## What's next

Per `docs/project-map.md` and the 2026-08-16 investigation, the clearest remaining work: pulling the full process-timeline fields (`inspectiondate`, `certifieddate`, `currentstatusdate`, …) into the production pipeline so "stale" vs. "active" can be stated directly; requiring description coherence for the Persistent/Chronic bucket counts, not just the narrative wording; guarding backlog age against genuinely pre-1990 records; and auditing the remaining `OrderNumber` codes.
