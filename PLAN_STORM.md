# scout — storm-chase scoring (spec)

## What this is

A second scoring profile alongside the golden-hour photo scout: **"where should we point the
truck today?"** It ranks candidate chase targets by SPC convective outlook risk, traded off
against drive time.

Two pieces of work, in this order:

1. **Refactor** `scout.py` into a shared engine + a photo profile. No behavior change.
2. **Add** `storm.py`, a second profile using SPC categorical outlooks.

Decisions already made: shared engine now (not duplicate-then-merge), and SPC categorical
outlook only for v1 (no probabilistic hazard layers yet).

---

## Part 1 — the shared engine refactor

Split the current single `scout.py` into three flat modules in the same directory. **Still not a
package** — no `__init__.py`, no `src/` layout, no setup.py.

```
engine.py     # shared plumbing
scout.py      # photo profile CLI  (existing behavior, unchanged)
storm.py      # storm profile CLI  (new)
```

### Moves into `engine.py`

- `load_config()`
- location CSV loading (generalize: take a filename, since storm uses a different file)
- `haversine_miles()`, `estimate_drive_minutes()`
- `_get_with_retry()` — the HTTP retry helper
- cache directory helpers (generalize beyond `points.json`)
- the ranked-table rendering + the "skipped/excluded" footer notes
- shared CLI arguments (`--max-drive`, `--top`)

### Stays in `scout.py`

- golden hour via `astral`
- the NWS gridpoint client (`get_gridpoint`, `parse_valid_time`, `parse_iso8601_duration`,
  `value_at`, `fetch_forecast`)
- the photo scoring curve
- `--date`

> Leave the NWS client in `scout.py` for now. It is genuinely photo-specific today. When a
> future storm predictor needs gridpoint data, move it to `engine.py` *then* — not preemptively.

### Config restructure

Profile-specific settings currently sit in top-level `[drive]` / `[scoring]`. Split them so both
profiles can set their own drive budget and weights. Shared physics stays top-level.

```toml
[home]                      # unchanged
[nws]                       # unchanged

[drive]                     # shared physics only
road_factor = 1.25
avg_mph = 50

[output]
top = 5

[photo]
max_drive_minutes = 120
ideal_sky_cover = 45
sky_tolerance = 55

[photo.weights]
sky = 0.6
precip = 0.25
drive = 0.15

[storm]
max_drive_minutes = 360     # chase days are long drives
cache_ttl_minutes = 15
targets_file = "chase_targets.csv"

[storm.risk_scores]         # see "the arguable part" below
TSTM = 0.15
MRGL = 0.35
SLGT = 0.70
ENH  = 1.00
MDT  = 0.85
HIGH = 0.75

[storm.weights]
risk = 0.65
drive = 0.35
```

`scout.py` must be updated to read from `[photo]` / `[photo.weights]`. **Its output for the same
inputs must not change** — that's the refactor's acceptance test.

---

## Part 2 — the SPC data source (verified 2026-09-07, don't re-derive)

I probed these live before writing this spec. These are confirmed facts, not guesses.

### Endpoints — all return 200

```
https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson
https://www.spc.noaa.gov/products/outlook/day2otlk_cat.nolyr.geojson
https://www.spc.noaa.gov/products/outlook/day3otlk_cat.nolyr.geojson
```

**Use `.nolyr`, not `.lyr`.** Verified: in `.nolyr` the risk polygons are *cut out* — mutually
exclusive. A point inside the SLGT area is **not** inside MRGL or TSTM. Exactly zero or one
polygon contains any given point.

Send the same `User-Agent` from `[nws] user_agent`. Be a polite client — this is one national
file per run, so one request total.

### Feature properties (real sample)

```json
{"DN": 4, "LABEL": "SLGT", "LABEL2": "Slight Risk",
 "VALID_ISO": "2026-09-07T16:30:00+00:00",
 "EXPIRE_ISO": "2026-09-08T12:00:00+00:00",
 "ISSUE_ISO":  "2026-09-07T16:12:00+00:00",
 "FORECASTER": "Hart/Thornton",
 "stroke": "#DDAA00", "fill": "#FFE066"}
```

- **`LABEL`** is the risk code — use this as the `[storm.risk_scores]` config key. It's stable
  and human-readable.
- **`DN`** is the numeric severity rank — use this only for *ordering*. Confirmed: `2=TSTM`,
  `3=MRGL`, `4=SLGT`. Inferred but unverified (no such risk existed on the sample day):
  `5=ENH`, `6=MDT`, `8=HIGH`. Since scoring keys off `LABEL`, a wrong `DN` guess can't corrupt a
  score — worst case it mis-orders, which only matters in the defensive max() below.
- `VALID_ISO` / `EXPIRE_ISO` / `ISSUE_ISO` are clean ISO-8601 with offset — `datetime.fromisoformat()`
  parses them directly. No interval-splitting like the NWS `validTime` mess.

### Three gotchas

- **Both `Polygon` and `MultiPolygon` appear in the same file.** Don't assume one. `shapely.geometry.shape()`
  handles both — just pass it the raw geometry dict.
- **GeoJSON coordinate order is `(longitude, latitude)`** — the reverse of how you'll have the
  lat/lon in the CSV. `Point(lon, lat)`, not `Point(lat, lon)`. This is the single easiest bug to
  ship here and it fails silently by putting every target in the ocean.
- **An unrecognized `LABEL`** (SPC adds/renames something) should warn and score as no-risk, not
  crash.

### Dependency

`shapely` — verified it installs clean into the venv on this machine and that
`shape(geom).contains(Point(lon, lat))` works. Add it to the README's install line.

Do **not** hand-roll ray casting. Polygons-with-holes plus MultiPolygon is exactly the subtle-bug
surface that isn't worth saving a dependency over.

### Caching

The outlook is one national file that updates a handful of times a day. Cache the raw response to
`.cache/spc_day{N}_cat.geojson` and refetch only if the file's mtime is older than
`[storm] cache_ttl_minutes` (default 15). Simple mtime check — no TTL framework.

Unlike the NWS gridpoint lookups, **this fetch is free per location**: one file answers every
candidate. That property matters later (see v2 notes).

---

## Part 3 — scoring

### The arguable part: risk category is a peaked curve, not a ladder

The naive model is "HIGH risk = best day." That's wrong for what you two are actually driving
toward, and it's the storm equivalent of the 45%-sky-cover insight.

High-end MDT/HIGH days frequently mean fast-moving squall lines and messy, rain-wrapped,
multi-storm chaos — genuinely dangerous, and often poor for *visual structure*. The photogenic
days, discrete supercells with clean sculpted structure and a visible base, cluster more in the
SLGT/ENH range. So the default curve peaks at **ENH** and eases back down through MDT and HIGH.

```toml
TSTM = 0.15   # non-severe, but can still make a nice lightning shot
MRGL = 0.35
SLGT = 0.70
ENH  = 1.00   # peak: best odds of discrete, photogenic supercells
MDT  = 0.85
HIGH = 0.75   # high-end, but often linear/messy and legitimately dangerous
```

**This table is the first thing to argue about with your son.** It encodes a photographer's bias
toward structure over severity. If he chases for different reasons, the numbers change and nothing
else in the code does — which is the point of putting them in config.

### Drive time weighs much heavier than in the photo profile

A HIGH risk six hours away is worth less than an ENH an hour away. Chase drives are long and the
payoff is uncertain, so drive time is 0.35 here versus 0.15 for photo.

```python
risk_score  = config["storm"]["risk_scores"][label]    # 0.0 if in no polygon
drive_score = max(0.0, 1.0 - (drive_minutes / max_drive_minutes))
total = w["risk"] * risk_score + w["drive"] * drive_score
```

Normalize to 0–100 for display, same as photo.

### No risk is a valid answer, not missing data

A target outside every polygon is a real result meaning "no severe threat here" → `risk_score = 0.0`,
displayed as `--`. That is **different** from a fetch failure. Don't conflate them in the footer
notes.

**Most days, nothing near Iowa will have any risk at all.** That is the common case, not an error.
When every target scores zero risk, say so plainly:

```
No severe weather risk in the Day 1 outlook for any target.
```

---

## Part 4 — the predictor seam (build this, but only one predictor)

Brian is bringing more datasets and wants to end up at a multi-predictor ensemble — several
independent signals blended, with their *agreement* itself being informative. Build the seam now
so predictor #2 doesn't force a rewrite. Keep it to a dataclass and a list. **No plugin registry,
no entry points, no dynamic discovery.**

```python
@dataclass
class PredictorResult:
    score: float        # 0..1
    confidence: float   # 0..1 — v1 always 1.0 when data is present
    detail: str         # short cell for the table, e.g. "ENH" or "--"

class Predictor(Protocol):
    name: str
    def prepare(self, ctx) -> None: ...                       # runs ONCE per run
    def score_location(self, lat, lon) -> PredictorResult | None: ...   # per target
```

The two-phase shape is load-bearing: `prepare()` fetches the one national outlook file;
`score_location()` is then a pure in-memory polygon test. Future gridded predictors (CAPE, shear,
model soundings) have the same shape — one bulk fetch, then cheap per-point lookups.

`None` means genuine no-data only. "No risk here" is a `PredictorResult(score=0.0, detail="--")`.

v1 ships exactly one predictor: `SPCCategoricalPredictor`. The blend function should already be
written as a weighted mean over a list, so adding the second one is config plus a class.

**Spread/agreement is not rendered in v1** — with one predictor it's trivially zero and would be
noise in the table. Add the column when predictor #2 lands.

---

## `chase_targets.csv`

Same columns as `locations.csv` (`name,lat,lon,notes`), quoted strings. Separate file because the
photo spots are the wrong geography — a chase target list is towns and road junctions across a
multi-state area, not four parks within an hour.

Seed with a handful of **clearly-marked placeholder rows** for chase-relevant towns across
Iowa / Nebraska / Kansas / Missouri. Do not invent precise coordinates and present them as real —
mark them `REPLACE ME` the way `locations.csv` was seeded. Brian and his son will fill in the real
list.

---

## CLI and output

```
python storm.py [--day 1|2|3] [--max-drive 360] [--top 5]
```

**`--day`, not `--date`.** SPC only publishes Day 1/2/3 outlooks — arbitrary dates aren't a thing
this data source can answer. Default `--day 1`. Reject anything outside 1–3 with a clear message
rather than 404ing against a URL that doesn't exist.

```
SPC Day 1 Convective Outlook
Issued 2026-09-07 16:12Z by Hart/Thornton - valid through 2026-09-08 12:00Z

 score       location  drive_min  risk
  88.2    Osceola, IA         41  SLGT
  74.5   Chariton, IA         28  MRGL
  61.0  Knoxville, IA         12  MRGL
  22.9    Ottumwa, IA         55  --

(2 target(s) excluded: beyond max drive time)
```

Surfacing `FORECASTER` and the issue time is worth it — outlooks get updated through the day, and
knowing *which* issuance you're looking at is normal chase-planning hygiene.

---

## Acceptance criteria

**Refactor (must be verifiable as a no-op):**
- [ ] `python scout.py` produces byte-identical output to the pre-refactor version for the same
      date and locations — capture it before you start and diff it after
- [ ] `--date`, `--max-drive`, `--top` still work on `scout.py`
- [ ] `.cache/points.json` is still read and written as before

**Storm:**
- [ ] `python storm.py` runs clean, prints the ranked table with the issuance header
- [ ] `--day 1`, `--day 2`, `--day 3` all fetch and parse; `--day 4` errors clearly
- [ ] A target in no polygon shows `--`, scores 0 risk, and is **not** reported as skipped
- [ ] A day with no risk anywhere prints the plain "no risk" message, exit code 0
- [ ] Verify by hand against the SPC map at spc.noaa.gov: pick a target you can see sitting
      inside a colored area and confirm the script reports the same category. **This is the
      lon/lat-order check** — if targets come back in the wrong polygons or all `--`, that's the
      bug.
- [ ] Both `Polygon` and `MultiPolygon` features parse (the sample file contained both)
- [ ] Second run within the TTL makes no network request

---

## Out of scope for v1

No probabilistic hazard layers (tornado/hail/wind %). No mesoanalysis parameters (CAPE, shear,
helicity). No model soundings / HRRR. No storm-mode or initiation-timing logic. No road-network
or terrain quality. No map output. No visit log.

### Noted for v2, deliberately not built yet

- **Grid targets instead of a fixed CSV.** Since the SPC fetch is free per point, testing a
  ~25-mile grid across the drive radius costs nothing extra and is how you'd actually *find* a
  target rather than checking a list someone typed. This is the natural next move, and it only
  works because of the one-fetch-many-points property noted above.
- **Brian's incoming datasets** plug in as additional `Predictor` implementations. Don't design
  for them until the actual data shape is in hand — the seam is the commitment, the schema is not.
