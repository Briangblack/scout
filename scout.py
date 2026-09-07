"""scout - golden hour photo location scout

Answers one question: "where should I drive tonight to shoot the sunset?"

Pulls tonight's golden hour window, estimates drive time to each candidate
location, fetches NWS sky cover / precip forecast for that window, scores
each location, and prints a ranked table.

See PLAN.md for the full spec.
"""

import argparse
import json
import math
import re
import sys
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from astral import LocationInfo
from astral.sun import golden_hour, SunDirection

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / ".cache"
POINTS_CACHE = CACHE_DIR / "points.json"

NWS_BASE = "https://api.weather.gov"
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
POLITE_DELAY_SECONDS = 0.5

ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


# ---------------------------------------------------------------------------
# config / locations
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def load_locations() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "locations.csv")
    required = {"name", "lat", "lon"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"locations.csv is missing required columns: {missing}")
    return df


# ---------------------------------------------------------------------------
# sun times
# ---------------------------------------------------------------------------

def evening_golden_hour(cfg: dict, target_date: date) -> tuple[datetime, datetime]:
    home = cfg["home"]
    location = LocationInfo(
        name="home",
        region="",
        timezone=home["timezone"],
        latitude=home["lat"],
        longitude=home["lon"],
    )
    return golden_hour(
        location.observer,
        date=target_date,
        direction=SunDirection.SETTING,
        tzinfo=location.tzinfo,
    )


# ---------------------------------------------------------------------------
# drive time estimate (haversine, not routed - see PLAN.md)
# ---------------------------------------------------------------------------

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_miles = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_miles * math.asin(math.sqrt(a))


def estimate_drive_minutes(cfg: dict, home_lat: float, home_lon: float, lat: float, lon: float) -> float:
    drive_cfg = cfg["drive"]
    miles = haversine_miles(home_lat, home_lon, lat, lon)
    road_miles = miles * drive_cfg["road_factor"]
    return (road_miles / drive_cfg["avg_mph"]) * 60


# ---------------------------------------------------------------------------
# NWS forecast
# ---------------------------------------------------------------------------

def _get_with_retry(url: str, headers: dict) -> dict:
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} from {url}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException,) as exc:
            last_error = exc
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url} after {RETRY_ATTEMPTS} attempts: {last_error}")


def _load_points_cache() -> dict:
    if POINTS_CACHE.exists():
        return json.loads(POINTS_CACHE.read_text())
    return {}


def _save_points_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    POINTS_CACHE.write_text(json.dumps(cache, indent=2))


def _points_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)},{round(lon, 4)}"


def get_gridpoint(lat: float, lon: float, headers: dict, cache: dict) -> dict:
    key = _points_key(lat, lon)
    if key in cache:
        return cache[key]

    data = _get_with_retry(f"{NWS_BASE}/points/{lat},{lon}", headers)
    props = data["properties"]
    grid = {"gridId": props["gridId"], "gridX": props["gridX"], "gridY": props["gridY"]}
    cache[key] = grid
    return grid


def parse_iso8601_duration(duration: str) -> timedelta:
    match = ISO8601_DURATION_RE.match(duration)
    if not match:
        raise ValueError(f"Unrecognized ISO8601 duration: {duration!r}")
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return timedelta(days=parts["days"], hours=parts["hours"], minutes=parts["minutes"], seconds=parts["seconds"])


def parse_valid_time(valid_time: str) -> tuple[datetime, datetime]:
    start_str, duration_str = valid_time.split("/")
    start = datetime.fromisoformat(start_str)
    duration = parse_iso8601_duration(duration_str)
    return start, start + duration


def value_at(values: list[dict], target: datetime) -> float | None:
    """Find the forecast value whose interval contains `target`. None if no
    matching interval is found, or if the matching interval's value is null."""
    for entry in values:
        start, end = parse_valid_time(entry["validTime"])
        if start <= target < end:
            return entry["value"]
    return None


def fetch_forecast(lat: float, lon: float, target: datetime, headers: dict, cache: dict) -> tuple[float | None, float | None]:
    grid = get_gridpoint(lat, lon, headers, cache)
    url = f"{NWS_BASE}/gridpoints/{grid['gridId']}/{grid['gridX']},{grid['gridY']}"
    data = _get_with_retry(url, headers)
    props = data["properties"]

    sky_cover = value_at(props["skyCover"]["values"], target)
    precip = value_at(props["probabilityOfPrecipitation"]["values"], target)
    return sky_cover, precip


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score_location(sky_cover: float, precip: float, drive_minutes: float, cfg: dict) -> float:
    scoring = cfg["scoring"]
    weights = scoring["weights"]

    ideal = scoring["ideal_sky_cover"]
    tolerance = scoring["sky_tolerance"]
    sky_score = max(0.0, 1.0 - abs(sky_cover - ideal) / tolerance)

    precip_score = 1.0 - (precip / 100.0)

    max_drive = cfg["drive"]["max_drive_minutes_effective"]
    drive_score = max(0.0, 1.0 - (drive_minutes / max_drive))

    total = weights["sky"] * sky_score + weights["precip"] * precip_score + weights["drive"] * drive_score
    return total * 100


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(cfg: dict) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Golden hour photo location scout")
    parser.add_argument("--date", type=str, default=None, help="Target date, YYYY-MM-DD (default: today)")
    parser.add_argument("--max-drive", type=int, default=cfg["drive"]["max_drive_minutes"], help="Max one-way drive minutes")
    parser.add_argument("--top", type=int, default=cfg["output"]["top"], help="Number of results to print")
    return parser.parse_args()


def main() -> int:
    cfg = load_config()
    args = parse_args(cfg)
    cfg["drive"]["max_drive_minutes_effective"] = args.max_drive

    home = cfg["home"]
    tz = ZoneInfo(home["timezone"])
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(tz).date()

    gh_start, gh_end = evening_golden_hour(cfg, target_date)

    locations = load_locations()

    locations["drive_min"] = locations.apply(
        lambda row: estimate_drive_minutes(cfg, home["lat"], home["lon"], row["lat"], row["lon"]),
        axis=1,
    )

    reachable = locations[locations["drive_min"] <= args.max_drive].copy()
    excluded_by_drive = len(locations) - len(reachable)

    headers = {"User-Agent": cfg["nws"]["user_agent"]}
    points_cache = _load_points_cache()

    results = []
    skipped_no_data = 0

    for i, (_, row) in enumerate(reachable.iterrows()):
        if i > 0:
            time.sleep(POLITE_DELAY_SECONDS)
        try:
            sky_cover, precip = fetch_forecast(row["lat"], row["lon"], gh_start, headers, points_cache)
        except RuntimeError as exc:
            print(f"warning: {row['name']}: {exc}", file=sys.stderr)
            skipped_no_data += 1
            continue

        if sky_cover is None or precip is None:
            skipped_no_data += 1
            continue

        score = score_location(sky_cover, precip, row["drive_min"], cfg)
        results.append({
            "score": round(score, 1),
            "location": row["name"],
            "drive_min": round(row["drive_min"]),
            "sky_%": round(sky_cover),
            "precip_%": round(precip),
        })

    _save_points_cache(points_cache)

    print(f"Golden hour: {gh_start.strftime('%Y-%m-%d %H:%M')} - {gh_end.strftime('%H:%M %Z')}\n")

    if results:
        df = pd.DataFrame(results).sort_values("score", ascending=False).reset_index(drop=True)
        print(df.head(args.top).to_string(index=False))
    else:
        print("No locations scored.")

    notes = []
    if excluded_by_drive:
        notes.append(f"{excluded_by_drive} location(s) excluded: beyond max drive time")
    if skipped_no_data:
        notes.append(f"{skipped_no_data} location(s) skipped: no forecast data")
    if notes:
        print("\n(" + "; ".join(notes) + ")")

    return 0


if __name__ == "__main__":
    sys.exit(main())
