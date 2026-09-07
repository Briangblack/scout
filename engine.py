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
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / ".cache"

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
POLITE_DELAY_SECONDS = 0.5


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
