"""Apply conservative, offline quality checks to Stage 3 geocodes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from pipeline.run_context import RunContext, finalize_run
except ModuleNotFoundError:  # Direct execution from scripts/.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import RunContext, finalize_run


INPUT_CSV = Path("data/processed/stage3_geocoded_listings.csv")
OUTPUT_CSV = Path("data/processed/stage3_geocoded_listings_qc.csv")
REVIEW_CSV = Path("data/processed/stage3_geocode_review_final.csv")


def _text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().lower()


def evaluate_geocode(
    row: pd.Series,
    *,
    confidence_threshold: float,
    allow_low_confidence: bool,
) -> tuple[bool, list[str]]:
    """Return map readiness and review reasons for one geocoded listing."""
    issues: list[str] = []
    status = _text(row.get("geocode_status"))
    result_type = _text(row.get("geocode_result_type"))
    match_type = _text(row.get("geocode_match_type"))
    city = _text(row.get("geocode_city"))
    country = _text(row.get("geocode_country_code"))
    latitude = pd.to_numeric(row.get("latitude"), errors="coerce")
    longitude = pd.to_numeric(row.get("longitude"), errors="coerce")
    confidence = pd.to_numeric(row.get("geocode_confidence"), errors="coerce")
    distance = pd.to_numeric(row.get("distance_to_western_km"), errors="coerce")

    if status != "ok":
        issues.append(f"geocode_status_{status or 'missing'}")
    if pd.isna(latitude) or pd.isna(longitude):
        issues.append("missing_lat_lng")
    if result_type == "city":
        issues.append("city_level_only")
    if match_type in {
        "match_by_city_or_disrict",
        "match_by_city_or_district",
        "match_by_city",
    }:
        issues.append("matched_only_to_city_or_district")
    if city and city != "london":
        issues.append("city_mismatch")
    if country != "ca":
        issues.append("country_mismatch")
    low_confidence = pd.isna(confidence) or confidence < confidence_threshold
    if low_confidence:
        issues.append("low_confidence")
    if pd.notna(distance) and distance > 25:
        issues.append("far_from_western")

    blocking = {
        issue
        for issue in issues
        if issue != "far_from_western"
        and not (allow_low_confidence and issue == "low_confidence")
    }
    return not blocking, issues


def apply_geocode_qc(
    input_csv: Path,
    output_csv: Path,
    review_csv: Path,
    *,
    confidence_threshold: float = 0.8,
    allow_low_confidence: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Read Stage 3 output, write canonical/review CSVs, and return metrics."""
    frame = pd.read_csv(input_csv)
    evaluations = [
        evaluate_geocode(
            row,
            confidence_threshold=confidence_threshold,
            allow_low_confidence=allow_low_confidence,
        )
        for _, row in frame.iterrows()
    ]
    frame["map_ready"] = [ready for ready, _ in evaluations]
    frame["geocode_quality_issue"] = [";".join(items) for _, items in evaluations]

    review = frame.loc[~frame["map_ready"]].copy()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    review_csv.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_csv, index=False)
    review.to_csv(review_csv, index=False)

    issue_text = frame["geocode_quality_issue"].fillna("")
    metrics = {
        "map_ready_count": int(frame["map_ready"].sum()),
        "review_required_count": len(review),
        "missing_address_count": int(
            frame.get("geocode_status", pd.Series(index=frame.index, dtype=str))
            .fillna("")
            .astype(str)
            .eq("missing_address")
            .sum()
        ),
        "failed_geocode_count": int(
            frame.get("geocode_status", pd.Series(index=frame.index, dtype=str))
            .fillna("")
            .astype(str)
            .isin(["error", "not_found", "cache_miss"])
            .sum()
        ),
        "low_confidence_count": int(issue_text.str.contains("low_confidence").sum()),
    }
    return frame, review, metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, default=INPUT_CSV)
    parser.add_argument("--output-csv", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--review-csv", type=Path, default=REVIEW_CSV)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--confidence-threshold", type=float, default=0.8)
    parser.add_argument("--allow-low-confidence", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    context: RunContext | None = None
    input_csv, output_csv, review_csv = (
        args.input_csv,
        args.output_csv,
        args.review_csv,
    )
    if args.run_dir:
        context = RunContext.open_for_cli(
            args.run_dir,
            resume=args.resume,
            overwrite=args.overwrite,
            command=sys.argv,
            configuration={
                "stage3_qc": {
                    "confidence_threshold": args.confidence_threshold,
                    "allow_low_confidence": args.allow_low_confidence,
                }
            },
        )
        input_csv = context.paths.stage3_geocoded
        output_csv = context.paths.stage3_canonical
        review_csv = context.paths.stage3_geocode_review
        context.manifest["configuration"]["stage3_qc"] = {
            "confidence_threshold": args.confidence_threshold,
            "allow_low_confidence": args.allow_low_confidence,
        }
        context.save()
        context.ensure_outputs_available(
            [output_csv, review_csv], allow_existing=args.overwrite
        )
        context.start_stage(
            "stage3_qc",
            input_paths=[input_csv],
            output_paths=[output_csv, review_csv],
        )

    try:
        frame, review, metrics = apply_geocode_qc(
            input_csv,
            output_csv,
            review_csv,
            confidence_threshold=args.confidence_threshold,
            allow_low_confidence=args.allow_low_confidence,
        )
        if context:
            warnings = (
                [f"{len(review)} listings require geocode review."]
                if len(review)
                else []
            )
            context.finish_stage(
                "stage3_qc",
                input_rows=len(frame),
                output_rows=len(frame),
                warnings=warnings,
                metrics=metrics,
            )
            finalize_run(context)
    except Exception as error:
        if context:
            context.fail_stage("stage3_qc", error)
        raise

    print(f"Saved QC dataset: {output_csv}")
    print(f"Saved geocode review: {review_csv}")
    print(f"Map ready: {metrics['map_ready_count']}/{len(frame)}")


if __name__ == "__main__":
    main()
