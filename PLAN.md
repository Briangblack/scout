# scout — v1 spec (golden hour photo scout)

## What this is

A command-line script that answers one question: **"Where should I drive tonight to shoot the sunset?"**

It takes a list of candidate locations, computes tonight's golden hour, pulls the NWS cloud-cover
forecast for each location at that time, scores them, and prints a ranked table.

This is v1 of a larger idea. Storm-chase scoring is a later sibling of the same engine, but **it is
explicitly out of scope here.** Build the script, not the platform.

## Environment

- Python: `C:\Users\brian\dev\.venv\Scripts\python.exe` (3.14.3)
- Already installed: `pandas`, `requests`, `sqlalchemy`, `httpx`, `tzdata`
- **Needs installing:** `astral` (sun position / golden hour math)
- Config parsing uses stdlib `tomllib` — no dependency needed
- Project root: `C:\Users\brian\dev\projects\scout`

## Files to create

```
scout/
  scout.py         # everything — single file for v1
  config.toml      # tunable settings + scoring weights
  locations.csv    # candidate spots
  .gitignore       # .venv, __pycache__, .cache/
```

**Do not create a package structure, a database, a web server, or a module split.** One file.
When it outgrows one file we'll split it deliberately.

## Behavior

```
python scout.py [--date YYYY-MM-DD] [--max-drive 120] [--top 5]
```

1. Read `config.toml` (home coordinates, scoring weights, drive assumptions).
2. Read `locations.csv` into a DataFrame.
3. Compute the **evening golden hour** for the home location on the target date (default: today).
   One golden-hour window for the whole run is fine — locations within a few hours' drive differ by
   only minutes, and that precision doesn't matter at v1.
4. Estimate drive time to each location; drop anything over `--max-drive`.
5. For each surviving location, fetch NWS sky cover and precipitation probability at the golden-hour
   start time.
6. Score each location.
7. Print the top N as a ranked table, sorted best-first.

## Data sources

### Sun times — `astral`, computed locally

```python
from astral import LocationInfo
from astral.sun import golden_hour, SunDirection
```

Use `SunDirection.SETTING` for the evening window. `golden_hour()` returns a `(start, end)` tuple of
timezone-aware datetimes. Config supplies the timezone (`America/Chicago`).

### Weather — NWS API (`api.weather.gov`)

Free, no API key. Two-step lookup:

1. `GET https://api.weather.gov/points/{lat},{lon}`
   → response `properties` contains `gridId`, `gridX`, `gridY`.
2. `GET https://api.weather.gov/gridpoints/{gridId}/{gridX},{gridY}`
   → response `properties.skyCover.values` and `properties.probabilityOfPrecipitation.values`

**Required header.** NWS asks every client to identify itself or they will return 403:

```python
{"User-Agent": config["nws"]["user_agent"]}
```

Put a placeholder in `config.toml` like `"scout/0.1 (your-email@example.com)"` with a comment telling
Brian to fill in his own contact. Do not hardcode an email address.

**Three gotchas to handle explicitly:**

- **`validTime` is an ISO 8601 *interval*,** formatted `"2026-09-07T18:00:00+00:00/PT1H"` — a start
  timestamp, a slash, then a duration. You must split on `/`, parse the timestamp, and parse the
  duration to know how long the value applies. Durations can be multi-hour (`PT6H`) or day-length
  (`P1DT3H`). Write a small parser; do not assume every entry is one hour.
- **Values are sparse.** The API only emits a new entry when the value *changes*, so to find the value
  at golden hour you must find the interval that *contains* that time, not an exact timestamp match.
- **`value` can be `null`** for entries beyond the forecast horizon. Treat null as "no data" and mark
  the location as unscored rather than crashing or scoring it zero.

**Cache the `/points` lookup.** Grid coordinates for a lat/lon never change. Write them to
`.cache/points.json` keyed by rounded `"lat,lon"` and read from there on subsequent runs. This is the
only caching v1 needs — do not cache forecasts.

**Retry on failure.** `api.weather.gov` intermittently returns 500s. Retry up to 3 times with a short
backoff. Sleep ~0.5s between locations to be a polite client.

### Drive time — estimated, not routed

v1 uses great-circle (haversine) distance × a fudge factor, divided by an assumed average speed from
config:

```
drive_minutes = (haversine_miles * road_factor) / avg_mph * 60
```

Defaults: `road_factor = 1.25`, `avg_mph = 50`.

Isolate this in a single `estimate_drive_minutes(home, dest)` function. v2 swaps it for a real
OpenRouteService isochrone call, and that should be a one-function change. **Do not call a routing
API in v1** — no signup, no key, no quota.

## Scoring — the interesting part

Keep every weight and curve in one clearly-marked section near the top of the file so Brian can tune
it without hunting.

### Sky cover is a peaked curve, not a minimum

This is the core domain insight and the thing that makes the script worth writing. A clear sky is a
*boring* sunset — there's nothing up there to catch the light. Full overcast is flat gray. The
dramatic ones happen in the middle, where there's enough cloud to light up and enough gap for the
light to reach it.

Peak the score around **45% sky cover**, falling off toward both 0% and 100%:

```python
sky_score = max(0.0, 1.0 - abs(sky_cover - IDEAL_SKY_COVER) / SKY_TOLERANCE)
```

With `IDEAL_SKY_COVER = 45`, `SKY_TOLERANCE = 55`. Both live in `config.toml`. Brian will want to tune
these against reality, which is the whole point.

### The other two terms

```python
precip_score = 1.0 - (probability_of_precipitation / 100.0)
drive_score  = 1.0 - (drive_minutes / max_drive_minutes)
```

### Total

Weighted sum, weights from config, defaulting to:

```toml
[scoring.weights]
sky    = 0.6
precip = 0.25
drive  = 0.15
```

Normalize the final score to 0–100 for readability.

## Output

Print a pandas DataFrame — Brian reads these fluently, no need for a formatting library.

```
Golden hour: 2026-09-07 19:12 – 19:47 CDT

  score  location              drive_min  sky_%  precip_%
   82.4  Ledges State Park            38     41         5
   71.0  Saylorville Overlook         22     67        10
   55.3  Neal Smith Prairie           45     88        15

  (2 locations skipped: no forecast data)
```

Include the golden-hour window in the header — it's the other thing he actually needs to know.

## `locations.csv`

Columns: `name,lat,lon,notes`

Seed it with 3–5 **clearly-marked placeholder rows** that Brian will replace with his own spots.
Do not invent precise coordinates and present them as real — use obvious placeholder values and a
comment in `PLAN.md`-style at the top of the file, or a `notes` value saying `REPLACE ME`.

Candidate central-Iowa spots worth suggesting to him by name (he supplies the coordinates): Ledges
State Park, Saylorville Lake overlooks, Neal Smith National Wildlife Refuge, Loess Hills.

## Acceptance criteria

- [ ] `python scout.py` runs clean with no arguments and prints a ranked table
- [ ] `--date`, `--max-drive`, and `--top` all work
- [ ] A location beyond `--max-drive` is excluded before any API call is made for it
- [ ] Sky cover of 45% scores higher than both 5% and 95%, all else equal — verify this by hand
- [ ] A `null` forecast value produces a "skipped" note, not a traceback
- [ ] Second run is visibly faster / makes fewer requests, because `/points` came from cache
- [ ] Deleting `.cache/` and re-running still works

## Explicitly out of scope for v1

No database. No web UI. No storm scoring. No isochrone API. No visit/photo log. No scheduling. No
notifications. No moon phase, no light pollution, no fall-color data, no event feeds.

All of those are real ideas for later — leaving them out is what makes this finishable this weekend.
