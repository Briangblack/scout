"""engine - shared plumbing for scout profiles (photo, storm, ...).

Config loading, location CSVs, drive-time estimation, HTTP retry, generic
file caching, and the ranked-table / footer-note rendering shared by every
profile's CLI. Profile-specific scoring and data fetching stay in each
profile's own module (scout.py, storm.py) - this module knows nothing about
sky cover or convective outlooks.
"""

import argparse
import json
import math
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd
import requests

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / ".cache"

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
POLITE_DELAY_SECONDS = 0.5

DRIVE_TIME_CACHE_FILE = "drive_times.json"
DEFAULT_OSRM_BASE = "https://router.project-osrm.org"


# ---------------------------------------------------------------------------
# config / locations
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def load_locations(filename: str) -> pd.DataFrame:
    df = pd.read_csv(ROOT / filename)
    required = {"name", "lat", "lon"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{filename} is missing required columns: {missing}")
    return df


# ---------------------------------------------------------------------------
# drive time: haversine estimate + real routing (see PLAN_ROUTING.md)
# ---------------------------------------------------------------------------

def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_miles = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_miles * math.asin(math.sqrt(a))


def estimate_drive_minutes_haversine(cfg: dict, home_lat: float, home_lon: float, lat: float, lon: float) -> float:
    """Great-circle distance x a road fudge factor. No longer the primary path,
    but still does two real jobs: the offline fallback when routing fails or
    is disabled, and (via a generous max-speed bound) the pre-routing filter
    that avoids calling the routing API for provably-unreachable targets."""
    drive_cfg = cfg["drive"]
    miles = haversine_miles(home_lat, home_lon, lat, lon)
    road_miles = miles * drive_cfg["road_factor"]
    return (road_miles / drive_cfg["avg_mph"]) * 60


class RouteProvider(Protocol):
    name: str

    def drive_minutes(self, origin: tuple[float, float],
                       destinations: list[tuple[float, float]]) -> list[float | None]: ...


class OSRMProvider:
    """Routes against the public OSRM demo server - keyless, real road
    routing, but a shared community instance with no uptime guarantee.
    One batched /table request answers every destination."""

    name = "osrm"

    def __init__(self, base_url: str, headers: dict):
        self.base_url = base_url
        self.headers = headers

    def drive_minutes(self, origin: tuple[float, float],
                       destinations: list[tuple[float, float]]) -> list[float | None]:
        if not destinations:
            return []

        # OSRM coordinates are lon,lat - reversed from how they're stored
        # everywhere else in this project.
        coords = [f"{origin[1]},{origin[0]}"] + [f"{lon},{lat}" for lat, lon in destinations]
        url = f"{self.base_url}/table/v1/driving/{';'.join(coords)}?sources=0&annotations=duration"
        data = get_with_retry(url, self.headers)

        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM returned {data.get('code')}: {data.get('message', '')}")

        # First row is durations from origin; first entry is origin->origin (0).
        durations = data["durations"][0][1:]
        return [d / 60 if d is not None else None for d in durations]


def _drive_cache_key(home_lat: float, home_lon: float, lat: float, lon: float) -> str:
    return f"{round(home_lat, 4)},{round(home_lon, 4)}|{round(lat, 4)},{round(lon, 4)}"


@dataclass
class DriveTimeResult:
    minutes: pd.Series    # NaN for targets excluded by the pre-routing distance filter
    estimated_count: int  # targets whose time came from the haversine fallback, not real routing


def compute_drive_minutes(cfg: dict, home_lat: float, home_lon: float,
                           targets_df: pd.DataFrame, max_drive_minutes: float) -> DriveTimeResult:
    """Drive time for every row in `targets_df`, using real routing when
    configured. Two-stage: a haversine lower bound first prefilters out
    anything provably beyond `max_drive_minutes` (great-circle distance is
    never longer than the real road route, so this can't wrongly exclude a
    reachable target), then only the survivors get routed - one batched
    request, cached permanently after. On any routing failure, affected
    targets fall back to the haversine estimate rather than crashing."""
    routing_cfg = cfg.get("routing", {})
    provider_name = routing_cfg.get("provider", "haversine")

    if provider_name != "osrm":
        # Explicit offline mode: reproduce the original per-row estimate for
        # every row, unconditionally - this is the escape hatch and must
        # match pre-routing behavior exactly.
        minutes = targets_df.apply(
            lambda row: estimate_drive_minutes_haversine(cfg, home_lat, home_lon, row["lat"], row["lon"]),
            axis=1,
        )
        return DriveTimeResult(minutes=minutes, estimated_count=0)

    prefilter_mph = routing_cfg.get("prefilter_max_mph", 80)
    lower_bound = targets_df.apply(
        lambda row: haversine_miles(home_lat, home_lon, row["lat"], row["lon"]) / prefilter_mph * 60,
        axis=1,
    )
    reachable_mask = lower_bound <= max_drive_minutes

    minutes = pd.Series(float("nan"), index=targets_df.index, dtype=float)
    survivors = targets_df[reachable_mask]
    if survivors.empty:
        return DriveTimeResult(minutes=minutes, estimated_count=0)

    cache = load_json_cache(DRIVE_TIME_CACHE_FILE)
    keys = {idx: _drive_cache_key(home_lat, home_lon, row["lat"], row["lon"])
            for idx, row in survivors.iterrows()}
    to_fetch = [idx for idx in survivors.index if keys[idx] not in cache]

    if to_fetch:
        headers = {"User-Agent": cfg["nws"]["user_agent"]}
        provider = OSRMProvider(routing_cfg.get("osrm_base", DEFAULT_OSRM_BASE), headers)
        fetch_rows = survivors.loc[to_fetch]
        try:
            destinations = list(zip(fetch_rows["lat"], fetch_rows["lon"]))
            durations = provider.drive_minutes((home_lat, home_lon), destinations)
            for idx, duration in zip(to_fetch, durations):
                if duration is not None:
                    cache[keys[idx]] = duration
            save_json_cache(DRIVE_TIME_CACHE_FILE, cache)
        except RuntimeError:
            pass  # leave uncached - the fallback loop below covers every miss

    estimated_count = 0
    for idx, row in survivors.iterrows():
        key = keys[idx]
        if key in cache:
            minutes.loc[idx] = cache[key]
        else:
            minutes.loc[idx] = estimate_drive_minutes_haversine(cfg, home_lat, home_lon, row["lat"], row["lon"])
            estimated_count += 1

    return DriveTimeResult(minutes=minutes, estimated_count=estimated_count)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get_with_retry(url: str, headers: dict, timeout: int):
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} from {url}")
            resp.raise_for_status()
            return resp
        except (requests.RequestException,) as exc:
            last_error = exc
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url} after {RETRY_ATTEMPTS} attempts: {last_error}")


def get_with_retry(url: str, headers: dict) -> dict:
    return _get_with_retry(url, headers, timeout=10).json()


def get_bytes_with_retry(url: str, headers: dict) -> bytes:
    return _get_with_retry(url, headers, timeout=20).content


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------

def load_json_cache(filename: str) -> dict:
    path = CACHE_DIR / filename
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json_cache(filename: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / filename).write_text(json.dumps(data, indent=2))


def cache_path(filename: str) -> Path:
    return CACHE_DIR / filename


def is_cache_fresh(path: Path, ttl_minutes: float) -> bool:
    """True if `path` exists and was modified within the last `ttl_minutes`."""
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_minutes * 60


# ---------------------------------------------------------------------------
# CLI / output
# ---------------------------------------------------------------------------

def add_shared_args(parser: argparse.ArgumentParser, default_max_drive: int, default_top: int) -> None:
    parser.add_argument("--max-drive", type=int, default=default_max_drive, help="Max one-way drive minutes")
    parser.add_argument("--top", type=int, default=default_top, help="Number of results to print")


def print_ranked_table(results: list[dict], top: int, empty_message: str = "No results scored.") -> None:
    if results:
        df = pd.DataFrame(results).sort_values("score", ascending=False).reset_index(drop=True)
        print(df.head(top).to_string(index=False))
    else:
        print(empty_message)


def print_notes(notes: list[str]) -> None:
    if notes:
        print("\n(" + "; ".join(notes) + ")")
