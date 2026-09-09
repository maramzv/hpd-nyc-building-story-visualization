# NYC Building Stories

A data visualization project exploring NYC's **Open HPD Violations** dataset (`csn4-vhvf`) to understand what public housing-violation records reveal about individual buildings and NYC's housing stock as a whole.

This is a data-exploration and storytelling project, transforming data to real insights: **evidence → pattern → story**. 

## The question

> **What is happening to NYC's buildings, and what patterns are visible in the city's housing violation records?**

## The user

> Anyone trying to understand NYC housing conditions through public data with an evidence-backed exploration of what the data shows.

## The insight we're after

Not "this building has 42 violations." We're after transformations like recurrence, persistence, severity, concentration, and change over time — the kind of patterns that turn a pile of records into a building's story.

## Data source

NYC Open Data — Open HPD Violations
https://data.cityofnewyork.us/Housing-Development/Open-HPD-Violations/csn4-vhvf/about_data

Dataset ID: `csn4-vhvf`

Queried live via the Socrata API. An app token is optional for public reads but increases the request/throttling allowance. 

## Current state

1. **Data discovery** — schema, scale, date range, building identifiers, violation categories/status. No 2M-row download; aggregate queries only.
2. **Analytical grain** — how reliably can records be grouped into buildings?
3. **Hypothesis testing** — test candidate patterns (recurrence, persistence, trend, concentration) against real aggregated data before assuming any are real.
4. **Visualization build** — narrative notebook/report built around whichever patterns actually held up.
5. **Write-up** — summarizing what the data supports.

</details>
