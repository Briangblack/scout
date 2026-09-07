# scout

Two CLI tools that share one engine: pull external data around a location, filter by drive
time, score, rank.

- **`scout.py`** — *"Where should I drive tonight to shoot the sunset?"* Golden hour photo
  location scout, using NWS sky-cover forecasts.
- **`storm.py`** — *"Where should we point the truck today?"* Storm-chase target scout, using
  SPC convective outlooks.

```
$ python scout.py
Golden hour: 2026-09-07 19:00 - 19:53 CDT

 score                            location  drive_min  sky_%  precip_%
  89.2 Neal Smith National Wildlife Refuge         34     39         0
  80.6                      Eagle Spotting         33     31         0
  78.6                  Horns Ferry Bridge         40     30         0
  77.9                   Ledges State Park         90     55         0

$ python storm.py
SPC Day 1 Convective Outlook
Issued 2026-09-07 16:12Z by Hart/Thornton - valid through 2026-09-08 12:00Z

 score              location  drive_min risk
  44.4       Oskaloosa, Iowa         44 TSTM
  43.4        Albert Lea, MN        193 TSTM
  43.3 Nebraska Crossing, NE        202 TSTM
  43.3        St. Joseph, MO        212 TSTM
  32.1            Salina, KS        421   --
```

`drive_min` is real road time now, not a straight-line guess — see below.

## The domain insight in each scoring curve

**Photo:** a clear sky is a boring sunset — nothing up there to catch the light. Full overcast is
flat gray. The dramatic ones happen at partial cloud cover, where there's enough cloud to light up
and enough gap for the light to reach it. The curve peaks around 45% cover and falls off toward
both extremes — tune it in `config.toml` under `[photo]`.

**Storm:** the curve peaks at **ENH**, not **HIGH**. High-end risk days often mean fast, messy
squall lines; the more photogenic discrete supercells cluster more in the SLGT/ENH range. That's a
photographer's bias, not a severity ranking — argue with it and retune `[storm.risk_scores]`.

## Setup

Requires Python 3.11+ (uses stdlib `tomllib`).

```bash
pip install pandas requests astral shapely
```

1. Edit `config.toml`:
   - `[home]` — your starting coordinates (a landmark near you is fine, doesn't
     need to be your exact address)
   - `[nws] user_agent` — api.weather.gov requires a contact string; put in
     your own email
2. Edit `locations.csv` (for `scout.py`) and/or `chase_targets.csv` (for `storm.py`) —
   `name,lat,lon,notes` per row
3. Run either:

```bash
python scout.py
python storm.py
```

## Usage

```
python scout.py [--date YYYY-MM-DD] [--max-drive 120] [--top 5]
python storm.py [--day 1|2|3] [--max-drive 360] [--top 5]
```

| Flag | Applies to | Default | Meaning |
|---|---|---|---|
| `--date` | scout | today | Target date for golden hour |
| `--day` | storm | `1` | SPC outlook day (1-3 only — that's all SPC publishes) |
| `--max-drive` | both | from `config.toml` | One-way drive minutes cutoff — targets beyond this are excluded before any API call |
| `--top` | both | from `config.toml` | How many ranked results to print |

## How it works

Both tools share `engine.py`: config/CSV loading, drive-time routing, HTTP retry, generic file
caching, and the ranked-table renderer. Each tool owns only its own data fetch and scoring curve.

**Drive time** is real road routing via the public [OSRM](https://project-osrm.org/) demo server
(keyless — no signup, no API key to leak from a public repo). Before routing anything, a
great-circle distance prefilter drops targets that are provably unreachable at a generous assumed
speed, so `--max-drive` still excludes far-away targets with zero network calls. Routed times are
cached permanently in `.cache/drive_times.json` (drive time between two fixed points doesn't
change on any timescale that matters here). If routing fails or is unreachable, affected targets
silently fall back to the old great-circle estimate and a footer note says so — a run never dies
because a community demo server is down. Set `[routing] provider = "haversine"` in `config.toml`
to skip real routing entirely (useful offline, e.g. before a chase with no signal). Full rationale
in `PLAN_ROUTING.md`.

**scout.py:**
1. Compute tonight's evening golden hour window locally via `astral` — no API, no key.
2. Drop anything beyond `--max-drive` before making any weather calls.
3. For each surviving location, look up its NWS forecast grid (cached in `.cache/points.json` —
   grid coordinates never change) and fetch sky cover + precipitation for the golden-hour start.
4. Score = weighted blend of sky-cover-closeness-to-45%, low precip chance, short drive time.

**storm.py:**
1. Fetch the SPC Day N categorical convective outlook (one national GeoJSON file, cached for
   `[storm] cache_ttl_minutes` since it only updates a few times a day).
2. For each target, point-in-polygon test against the outlook's risk areas (TSTM/MRGL/SLGT/ENH/
   MDT/HIGH — cut out, not nested, so a point matches at most one).
3. Score = weighted blend of risk-category score and drive time. A target outside every risk area
   scores 0 risk (shown as `--`) — that's a real result, not missing data. See `PLAN_STORM.md`.

Built with a predictor seam (`Predictor.prepare()` / `.score_location()`) so a second data source
— a probabilistic hazard layer, a mesoanalysis parameter — plugs in as another predictor blended
into the same score, without restructuring `storm.py`.

## What this isn't (yet)

No database, no web UI, no isochrone-based grid search, no visit/photo log, no scheduling, no
probabilistic hazard layers, no mesoanalysis parameters. See `PLAN.md`, `PLAN_STORM.md`, and
`PLAN_ROUTING.md` for the full specs and the reasoning behind what got left out.
