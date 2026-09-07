"""storm - storm-chase target scout

Answers one question: "where should we point the truck today?"

Ranks candidate chase targets by SPC categorical convective outlook risk,
traded off against drive time. See PLAN_STORM.md for the full spec.

Built as a predictor seam rather than a single hardcoded scoring function:
Predictor.prepare() does one bulk fetch per run (the SPC outlook is a single
national file), then Predictor.score_location() is a cheap per-point lookup
against data already in memory. v1 ships exactly one predictor
(SPCCategoricalPredictor); the blend() function already averages over a list
so a second predictor (e.g. a mesoanalysis parameter) is config + a class,
not a rewrite.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from shapely.geometry import Point, shape

import engine

SPC_BASE = "https://www.spc.noaa.gov/products/outlook"


# ---------------------------------------------------------------------------
# predictor seam
# ---------------------------------------------------------------------------

@dataclass
class PredictorResult:
    score: float        # 0..1
    confidence: float   # 0..1 - v1 always 1.0 when data is present
    detail: str         # short cell for the table, e.g. "ENH" or "--"


@dataclass
class Context:
    day: int
    headers: dict
    cfg: dict


class Predictor(Protocol):
    name: str

    def prepare(self, ctx: Context) -> None: ...
    def score_location(self, lat: float, lon: float) -> PredictorResult | None: ...


@dataclass
class SPCCategoricalPredictor:
    """SPC Day 1/2/3 categorical convective outlook (TSTM..HIGH).

    Uses the `.nolyr` GeoJSON - verified the risk polygons there are cut out
    (mutually exclusive), so a point is contained by at most one feature.
    """

    name: str = "spc_categorical"
    features: list[tuple[str, object]] = field(default_factory=list)
    risk_scores: dict[str, float] = field(default_factory=dict)
    issued: str | None = None
    valid: str | None = None
    expire: str | None = None
    forecaster: str | None = None

    def prepare(self, ctx: Context) -> None:
        self.risk_scores = ctx.cfg["storm"]["risk_scores"]

        cache_file = engine.cache_path(f"spc_day{ctx.day}_cat.geojson")
        ttl = ctx.cfg["storm"]["cache_ttl_minutes"]

        if engine.is_cache_fresh(cache_file, ttl):
            raw = cache_file.read_bytes()
        else:
            url = f"{SPC_BASE}/day{ctx.day}otlk_cat.nolyr.geojson"
            raw = engine.get_bytes_with_retry(url, ctx.headers)
            engine.CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_bytes(raw)

        data = json.loads(raw)
        self.features = []
        for f in data["features"]:
            props = f["properties"]
            self.features.append((props["LABEL"], shape(f["geometry"])))
            # every feature in a given file shares the same issuance metadata
            self.issued = props.get("ISSUE_ISO")
            self.valid = props.get("VALID_ISO")
            self.expire = props.get("EXPIRE_ISO")
            self.forecaster = props.get("FORECASTER")

    def score_location(self, lat: float, lon: float) -> PredictorResult:
        # GeoJSON coordinate order is (longitude, latitude) - reversed from
        # how lat/lon are stored everywhere else in this project.
        point = Point(lon, lat)
        for label, geom in self.features:
            if geom.contains(point):
                score = self.risk_scores.get(label)
                if score is None:
                    print(f"warning: unrecognized SPC risk label {label!r}, scoring as no-risk", file=sys.stderr)
                    return PredictorResult(score=0.0, confidence=1.0, detail="--")
                return PredictorResult(score=score, confidence=1.0, detail=label)
        return PredictorResult(score=0.0, confidence=1.0, detail="--")


def blend(weighted_results: list[tuple[float, PredictorResult]]) -> float:
    """Weighted mean of predictor scores. `weighted_results` is a list of
    (weight, PredictorResult) pairs. v1 has exactly one predictor; this
    already generalizes to an ensemble without a rewrite."""
    total_weight = sum(w for w, _ in weighted_results)
    if total_weight == 0:
        return 0.0
    return sum(w * r.score for w, r in weighted_results) / total_weight


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score_target(risk_score: float, drive_minutes: float, max_drive_minutes: float, weights: dict) -> float:
    drive_score = max(0.0, 1.0 - (drive_minutes / max_drive_minutes))
    total = weights["risk"] * risk_score + weights["drive"] * drive_score
    return total * 100


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def format_utc(iso_str: str) -> str:
    return datetime.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M") + "Z"


def print_header(day: int, predictor: SPCCategoricalPredictor) -> None:
    print(f"SPC Day {day} Convective Outlook")
    if predictor.issued:
        print(f"Issued {format_utc(predictor.issued)} by {predictor.forecaster} "
              f"- valid through {format_utc(predictor.expire)}\n")
    else:
        print("(no risk areas issued for this outlook)\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(cfg: dict) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Storm-chase target scout (SPC categorical outlook)")
    parser.add_argument("--day", type=int, default=1, choices=[1, 2, 3], help="SPC outlook day (1-3)")
    engine.add_shared_args(parser, cfg["storm"]["max_drive_minutes"], cfg["output"]["top"])
    return parser.parse_args()


def main() -> int:
    cfg = engine.load_config()
    args = parse_args(cfg)
    storm_cfg = cfg["storm"]
    home = cfg["home"]
    headers = {"User-Agent": cfg["nws"]["user_agent"]}

    predictor = SPCCategoricalPredictor()
    ctx = Context(day=args.day, headers=headers, cfg=cfg)
    try:
        predictor.prepare(ctx)
    except RuntimeError as exc:
        print(f"error: could not fetch SPC Day {args.day} outlook: {exc}", file=sys.stderr)
        return 1

    targets = engine.load_locations(storm_cfg["targets_file"])

    drive_result = engine.compute_drive_minutes(cfg, home["lat"], home["lon"], targets, args.max_drive)
    targets["drive_min"] = drive_result.minutes

    reachable = targets[targets["drive_min"] <= args.max_drive].copy()
    excluded_by_drive = len(targets) - len(reachable)

    results = []
    for _, row in reachable.iterrows():
        pred_result = predictor.score_location(row["lat"], row["lon"])
        risk_score = blend([(1.0, pred_result)])
        score = score_target(risk_score, row["drive_min"], args.max_drive, storm_cfg["weights"])
        results.append({
            "score": round(score, 1),
            "location": row["name"],
            "drive_min": round(row["drive_min"]),
            "risk": pred_result.detail,
        })

    print_header(args.day, predictor)

    any_risk = any(r["risk"] != "--" for r in results)
    if results and not any_risk:
        print(f"No severe weather risk in the Day {args.day} outlook for any target.")
    else:
        engine.print_ranked_table(results, args.top, empty_message="No targets scored.")

    notes = []
    if excluded_by_drive:
        notes.append(f"{excluded_by_drive} target(s) excluded: beyond max drive time")
    if drive_result.estimated_count:
        notes.append(f"drive times for {drive_result.estimated_count} target(s) estimated - routing unavailable")
    engine.print_notes(notes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
