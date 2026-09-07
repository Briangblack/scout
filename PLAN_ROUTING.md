# scout — real drive-time routing (spec)

## What this is

Replace the great-circle drive-time estimate with real road routing, for both profiles.

The current estimate is `haversine_miles × road_factor ÷ avg_mph`. It is wrong by a
*non-constant* margin, so no amount of tuning `road_factor` fixes it.

Measured 2026-09-07 against the live chase target list:

| target | haversine | real roads | error |
|---|---|---|---|
| Oskaloosa, IA | 50.2 | 44.5 | +12.8% |
| Nebraska Crossing, NE | 234.3 | 201.5 | +16.2% |
| Salina, KS | 442.1 | 420.6 | +5.1% |
| St. Joseph, MO | 208.4 | 212.1 | −1.8% |
| Albert Lea, MN | 233.8 | 192.8 | +21.2% |

The spread is the argument. Error depends on whether a good interstate line exists to the target,
which a straight line cannot know. It is already producing wrong output: Nebraska Crossing and
Albert Lea both rendered as `234 min / score 43.1` — a **fake tie** created entirely by estimate
error. By road they differ by 9 minutes, and Albert Lea is 41 minutes closer than reported.

---

## Provider decision: OSRM now, ORS later

I recommended OpenRouteService last turn. **I've changed that recommendation** — here's why.

I probed both. Findings:

- **OSRM public demo** (`router.project-osrm.org`) — verified working, **keyless**. Its `/table`
  endpoint returns durations from one origin to many destinations in a single request. Every
  number in the table above came from it.
- **OpenRouteService** — verified the endpoint exists (`POST /v2/matrix/driving-car` returns
  `401 {"error": "Authorization field missing"}`, confirming the path and that auth is an
  `Authorization` field). Could **not** verify request/response shapes, because that needs a key.

OSRM ships real routing today with zero signup and — importantly for a public repo — **zero secret
management**. An API key in a public GitHub repo gets scraped and abused, so not needing one is a
genuine security win, not just convenience.

What ORS buys that OSRM's demo doesn't: **isochrones**. That's the primitive the grid-search idea
in `PLAN_STORM.md` needs — one call returns a "everywhere within N minutes" polygon, and then
point-in-polygon tests are free, exactly like the SPC predictor already works (and `shapely` is
already a dependency). Point-to-point routing is O(n) API calls in target count; isochrones are
O(1) regardless.

**So:** ship OSRM behind a provider seam. Add ORS when isochrones actually buy something. Don't
pay for key management until then.

### OSRM demo etiquette

It is a free community server, explicitly not intended for heavy or production use. This design
makes **one request per run, cached permanently after** — that's about as light as it gets. If this
ever grows into a scheduled job or a web app, move to a self-hosted OSRM or ORS. Note it in the
README, don't build for it now.

---

## Part 1 — batch the call

Routing is a **batch** operation, unlike the current per-pair function. One request gets every
target's duration.

### Verified endpoint

```
GET https://router.project-osrm.org/table/v1/driving/{lon,lat};{lon,lat};...?sources=0&annotations=duration
```

- Coordinates are **`lon,lat`**, semicolon-separated — the same reversal that bites in GeoJSON.
  Home goes first.
- `sources=0` means "durations from the first coordinate only."
- Response: `{"durations": [[0, secs, secs, ...]]}` — a single row, first entry is home→home = 0,
  so **skip index 0** when zipping back to targets.
- Durations are in **seconds**. Divide by 60.
- An unroutable pair comes back `null` — treat as no-data and fall back for that target only.

Coordinates go in the URL path, so a very long target list could hit URL length limits. At the
current scale (5–20) this is a non-issue. If the list ever passes ~100, chunk the request. Don't
build chunking now.

### New engine API

Replace the per-row function with a batch one:

```python
def compute_drive_minutes(cfg, home_lat, home_lon, targets_df) -> pd.Series
```

Both callers change from:

```python
df["drive_min"] = df.apply(lambda row: engine.estimate_drive_minutes(...), axis=1)
```

to:

```python
df["drive_min"] = engine.compute_drive_minutes(cfg, home["lat"], home["lon"], df)
```

Keep `haversine_miles()` and the existing estimate — rename it `estimate_drive_minutes_haversine()`
to make its role explicit. It is no longer the primary path but it is **not** dead code; it has two
real jobs below.

### Provider seam

```python
class RouteProvider(Protocol):
    name: str
    def drive_minutes(self, origin: tuple[float, float],
                      destinations: list[tuple[float, float]]) -> list[float | None]: ...
```

Ship exactly one implementation: `OSRMProvider`. `None` in the returned list means "couldn't route
this one." **Do not write an `ORSProvider` stub** — untestable code for an endpoint whose response
shape I couldn't verify is worse than no code. Document in the README that ORS slots in here.

Provider selection goes in config so switching later is a config edit:

```toml
[routing]
provider = "osrm"           # "osrm" | "haversine"
prefilter_max_mph = 80
osrm_base = "https://router.project-osrm.org"
```

Setting `provider = "haversine"` must fully restore today's offline behavior. That's the escape
hatch if the demo server is down or you're chasing without signal.

---

## Part 2 — haversine becomes the prefilter

Both existing specs have an acceptance criterion that targets beyond `--max-drive` are excluded
**before** any API call. Real routing appears to invert that — you need a call to know the time.

It doesn't, because **great-circle distance is a strict lower bound on road distance.** No road
route is shorter than the straight line. So:

```python
min_possible_minutes = haversine_miles / prefilter_max_mph * 60
if min_possible_minutes > max_drive:
    exclude   # provably unreachable, no routing call needed
```

Two things to get right:

- Use a **generous** `prefilter_max_mph` (default 80), **not** `avg_mph`. The bound must assume the
  fastest plausible average speed, or you'll wrongly exclude a target that's reachable via
  interstate. At 50 mph the bound is not sound.
- Do **not** apply `road_factor` here. It inflates the distance, which would over-exclude.

Route only the survivors. This preserves the "no API calls for far-away targets" property and is
provably free of false exclusions.

---

## Part 3 — cache drive times permanently

Drive time between two fixed points changes on the timescale of road construction. Cache it and
effectively never refetch.

- File: `.cache/drive_times.json`, same pattern as `points.json`
- Key: `f"{round(olat,4)},{round(olon,4)}|{round(dlat,4)},{round(dlon,4)}"`
- Value: minutes (float)
- **No TTL.** Delete the file to bust it. A TTL here would be ceremony.

Consequence: after the first run, routing costs **zero** requests until you add a target or move
home. Only cache misses go into the batch call. If every target hits cache, make no request at all.

---

## Part 4 — degrade, never crash

A chase-day run must not die because a demo server is down.

On any routing failure — network error, 5xx, timeout, `null` duration — fall back to
`estimate_drive_minutes_haversine()` **for the affected targets only**, and say so:

```
(drive times for 2 target(s) estimated - routing unavailable)
```

Route through the existing `engine.print_notes()` so it renders with the other footer notes. This
matters beyond robustness: a chaser needs to know whether `193 min` is measured or guessed. Never
silently mix the two.

---

## Acceptance criteria

- [ ] Both `scout.py` and `storm.py` report real road drive times
- [ ] Albert Lea comes back ≈193 min, not ≈234 — and no longer ties Nebraska Crossing
- [ ] One routing request per run on a cold cache; **zero** on a warm one (check via mtime or a
      request counter, the way the SPC cache was verified)
- [ ] With the network unreachable, both scripts still produce a ranked table plus the
      "estimated" footer note, exit 0
- [ ] `provider = "haversine"` reproduces today's numbers exactly
- [ ] A target beyond `--max-drive` is prefiltered out with no routing call made for it
- [ ] Prefilter never wrongly excludes: set `--max-drive` just above a known real drive time and
      confirm the target survives
- [ ] Deleting `.cache/` and re-running still works

---

## Out of scope

No isochrones. No ORS implementation. No turn-by-turn directions, no multi-stop route
optimization, no traffic/time-of-day awareness, no chunking for long target lists.

### Noted, deliberately not bundled here

- **`config.local.toml` overlay.** A gitignored local config that overrides the committed one
  would let you keep exact home coordinates private while the public repo shows the Casey's
  approximation. Genuinely useful, unrelated to routing — its own PR.
- **Isochrone grid search.** The real prize (see `PLAN_STORM.md`). Needs ORS, and therefore needs
  the key-handling story that this spec deliberately defers.
- **Attribution.** OSRM/ORS are OpenStreetMap-derived. A CLI printing numbers is fine; a public
  web app would need ODbL attribution. Relevant only if this ever ships a UI.
