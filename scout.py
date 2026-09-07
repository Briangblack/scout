"""scout - golden hour photo location scout

Answers one question: "where should I drive tonight to shoot the sunset?"

Pulls tonight's golden hour window, estimates drive time to each candidate
location, fetches NWS sky cover / precip forecast for that window, scores
each location, and prints a ranked table.

See PLAN.md for the full spec.
"""

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import golden_hour, SunDirection

import engine

NWS_BASE = "https://api.weather.gov"
POINTS_CACHE_FILE = "points.json"

ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


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
# NWS forecast
# ---------------------------------------------------------------------------

def get_gridpoint(lat: float, lon: float, headers: dict, cache: dict) -> dict:
    key = f"{round(lat, 4)},{round(lon, 4)}"
    if key in cache:
        return cache[key]

    data = engine.get_with_retry(f"{NWS_BASE}/points/{lat},{lon}", headers)
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
    data = engine.get_with_retry(url, headers)
    props = data["properties"]

    sky_cover = value_at(props["skyCover"]["values"], target)
    precip = value_at(props["probabilityOfPrecipitation"]["values"], target)
    return sky_cover, precip


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score_location(sky_cover: float, precip: float, drive_minutes: float, max_drive_minutes: float, photo_cfg: dict) -> float:
    weights = photo_cfg["weights"]

    ideal = photo_cfg["ideal_sky_cover"]
    tolerance = photo_cfg["sky_tolerance"]
    sky_score = max(0.0, 1.0 - abs(sky_cover - ideal) / tolerance)

    precip_score = 1.0 - (precip / 100.0)

    drive_score = max(0.0, 1.0 - (drive_minutes / max_drive_minutes))

    total = weights["sky"] * sky_score + weights["precip"] * precip_score + weights["drive"] * drive_score
    return total * 100


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(cfg: dict) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Golden hour photo location scout")
    parser.add_argument("--date", type=str, default=None, help="Target date, YYYY-MM-DD (default: today)")
    engine.add_shared_args(parser, cfg["photo"]["max_drive_minutes"], cfg["output"]["top"])
    return parser.parse_args()


def main() -> int:
    cfg = engine.load_config()
    args = parse_args(cfg)

    home = cfg["home"]
    tz = ZoneInfo(home["timezone"])
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(tz).date()

    gh_start, gh_end = evening_golden_hour(cfg, target_date)

    locations = engine.load_locations("locations.csv")

    drive_result = engine.compute_drive_minutes(cfg, home["lat"], home["lon"], locations, args.max_drive)
    locations["drive_min"] = drive_result.minutes

    reachable = locations[locations["drive_min"] <= args.max_drive].copy()
    excluded_by_drive = len(locations) - len(reachable)

    headers = {"User-Agent": cfg["nws"]["user_agent"]}
    points_cache = engine.load_json_cache(POINTS_CACHE_FILE)

    results = []
    skipped_no_data = 0

    for i, (_, row) in enumerate(reachable.iterrows()):
        if i > 0:
            time.sleep(engine.POLITE_DELAY_SECONDS)
        try:
            sky_cover, precip = fetch_forecast(row["lat"], row["lon"], gh_start, headers, points_cache)
        except RuntimeError as exc:
            print(f"warning: {row['name']}: {exc}", file=sys.stderr)
            skipped_no_data += 1
            continue

        if sky_cover is None or precip is None:
            skipped_no_data += 1
            continue

        score = score_location(sky_cover, precip, row["drive_min"], args.max_drive, cfg["photo"])
        results.append({
            "score": round(score, 1),
            "location": row["name"],
            "drive_min": round(row["drive_min"]),
            "sky_%": round(sky_cover),
            "precip_%": round(precip),
        })

    engine.save_json_cache(POINTS_CACHE_FILE, points_cache)

    print(f"Golden hour: {gh_start.strftime('%Y-%m-%d %H:%M')} - {gh_end.strftime('%H:%M %Z')}\n")

    engine.print_ranked_table(results, args.top, empty_message="No locations scored.")

    notes = []
    if excluded_by_drive:
        notes.append(f"{excluded_by_drive} location(s) excluded: beyond max drive time")
    if drive_result.estimated_count:
        notes.append(f"drive times for {drive_result.estimated_count} location(s) estimated - routing unavailable")
    if skipped_no_data:
        notes.append(f"{skipped_no_data} location(s) skipped: no forecast data")
    engine.print_notes(notes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
