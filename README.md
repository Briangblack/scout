# scout

Answers one question: **"Where should I drive tonight to shoot the sunset?"**

Pulls tonight's golden hour window, estimates drive time to a list of candidate
locations, fetches the NWS sky-cover and precipitation forecast for each, scores
them, and prints a ranked table.

```
Golden hour: 2026-09-07 19:00 - 19:53 CDT

 score                            location  drive_min  sky_%  precip_%
  91.3 Neal Smith National Wildlife Refuge         17     39         0
  81.9                      Eagle Spotting         23     31         0
  80.4                  Horns Ferry Bridge         26     30         0
  79.5                   Ledges State Park         77     55         0
```

## Why sky cover peaks in the middle, not at 0%

A clear sky is a boring sunset — nothing up there to catch the light. Full
overcast is flat gray. The dramatic ones happen at partial cloud cover, where
there's enough cloud to light up and enough gap for the light to reach it.
The scoring curve peaks around 45% cover and falls off toward both extremes —
tune it in `config.toml` under `[scoring]`.

## Setup

Requires Python 3.11+ (uses stdlib `tomllib`).

```bash
pip install pandas requests astral
```

1. Edit `config.toml`:
   - `[home]` — your starting coordinates (a landmark near you is fine, doesn't
     need to be your exact address)
   - `[nws] user_agent` — api.weather.gov requires a contact string; put in
     your own email
2. Edit `locations.csv` — one row per candidate spot: `name,lat,lon,notes`
3. Run it:

```bash
python scout.py
```

## Usage

```
python scout.py [--date YYYY-MM-DD] [--max-drive 120] [--top 5]
```

| Flag | Default | Meaning |
|---|---|---|
| `--date` | today | Target date for golden hour |
| `--max-drive` | from `config.toml` | One-way drive minutes cutoff — locations beyond this are excluded before any API call |
| `--top` | from `config.toml` | How many ranked results to print |

## How it works

1. Compute tonight's evening golden hour window locally via `astral` — no API,
   no key.
2. Estimate drive time to each location via great-circle distance × a road
   fudge factor (not real routing — see `PLAN.md` for why, and what v2 swaps
   in instead).
3. Drop anything beyond `--max-drive` before making any weather calls.
4. For each surviving location, look up its NWS forecast grid (cached in
   `.cache/points.json` — grid coordinates never change) and fetch sky cover +
   precipitation probability for the golden-hour start time.
5. Score = weighted blend of sky-cover-closeness-to-45%, low precip chance,
   and short drive time. Weights live in `config.toml`.
6. Print ranked results; note any locations skipped for missing forecast data.

## What this isn't (yet)

No database, no web UI, no storm-chase scoring, no real routing API, no
visit/photo log, no scheduling. See `PLAN.md` for the full v1 spec and the
reasoning behind what got left out.
