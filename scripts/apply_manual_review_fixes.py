import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

try:
    from pipeline.run_context import RunContext
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.run_context import RunContext


INPUT_CSV = Path("data/processed/stage2_ai_enriched.csv")
OUTPUT_CSV = Path("data/processed/stage2_ai_enriched_reviewed.csv")


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> None:
    for col in columns:
        if col not in df.columns:
            df[col] = None


def patch_listing(df: pd.DataFrame, listing_id: str, updates: dict, note: str) -> int:
    mask = df["listing_id"].astype(str) == str(listing_id)
    if not mask.any():
        print(f"WARNING: listing_id {listing_id} not found")
        return 0
    for col, value in updates.items():
        if col not in df.columns:
            df[col] = None
        df.loc[mask, col] = value
    df.loc[mask, "manual_reviewed"] = True
    df.loc[mask, "manual_review_note"] = note
    print(f"Patched listing {listing_id}: {note}")
    return int(mask.sum())


def apply_manual_fixes(input_csv: Path, output_csv: Path) -> tuple[pd.DataFrame, int]:
    df = pd.read_csv(input_csv)
    ensure_columns(
        df,
        [
            "manual_reviewed",
            "manual_review_note",
            "is_sublet_source",
            "lease_type_source",
            "bathroom_type_source",
            "utilities_included_source",
            "utilities_status_source",
            "price_numeric_source",
        ],
    )
    fixes = [
        (
            "53374",
            {
                "is_sublet": False,
                "is_sublet_source": "manual_review",
                "lease_type": "standard",
                "lease_type_source": "manual_review",
            },
            "Listing is a standard full-year lease; description only mentions option to sublease later.",
        ),
        (
            "54910",
            {"bathroom_type": "unknown", "bathroom_type_source": "manual_review"},
            "Bathroom setup is mixed/ambiguous; do not mark as private.",
        ),
        (
            "61105",
            {
                "utilities_included": None,
                "utilities_included_source": "manual_review",
                "utilities_status": "unknown",
                "utilities_status_source": "manual_review",
            },
            "Utilities depend on selected price option; not cleanly all-included.",
        ),
        (
            "61672",
            {
                "utilities_included": False,
                "utilities_included_source": "manual_review",
                "utilities_status": "not_included",
                "utilities_status_source": "manual_review",
            },
            "Displayed $850 price is plus utilities; utilities-included option has different price.",
        ),
        (
            "54479",
            {
                "utilities_included": False,
                "utilities_included_source": "manual_review",
                "utilities_status": "not_included",
                "utilities_status_source": "manual_review",
            },
            "Displayed $665 price is plus utilities; all-inclusive option has different price.",
        ),
        (
            "62164",
            {
                "price_numeric": None,
                "price_monthly": None,
                "price_numeric_source": "manual_review",
            },
            "$1 appears to be a placeholder price; actual rent is unclear.",
        ),
        (
            "62172",
            {"bathroom_type": "shared", "bathroom_type_source": "manual_review"},
            "Description indicates bathroom is shared with another room.",
        ),
    ]
    corrected_rows = sum(
        patch_listing(df, listing_id, updates, note)
        for listing_id, updates, note in fixes
    )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)
    print(f"\nSaved reviewed dataset -> {output_csv}")
    return df, corrected_rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply known manual listing corrections.")
    parser.add_argument("--input-csv", type=Path, default=INPUT_CSV)
    parser.add_argument("--output-csv", type=Path, default=OUTPUT_CSV)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.resume and args.overwrite:
        parser.error("--resume and --overwrite are mutually exclusive")
    if (args.resume or args.overwrite) and args.run_dir is None:
        parser.error("--resume and --overwrite require --run-dir")

    context = None
    if args.run_dir is not None:
        if args.input_csv != INPUT_CSV or args.output_csv != OUTPUT_CSV:
            parser.error("Explicit input/output paths cannot be combined with --run-dir")
        context = RunContext.open_for_cli(
            args.run_dir,
            resume=args.resume,
            overwrite=args.overwrite,
            command=sys.argv,
            configuration={"manual_fixes": {"skip": args.skip}},
        )
        input_csv = context.paths.stage2_enriched
        output_csv = context.paths.stage2_reviewed
        context.ensure_outputs_available(
            [output_csv], allow_existing=args.resume or args.overwrite
        )
        if not input_csv.exists():
            raise FileNotFoundError(f"Stage 2 enriched output not found: {input_csv}")
        context.manifest["configuration"]["manual_fixes"] = {"skip": args.skip}
        context.save()
    else:
        input_csv = args.input_csv
        output_csv = args.output_csv

    input_rows = len(pd.read_csv(input_csv))
    if args.skip:
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(input_csv, output_csv)
        if context is not None:
            context.skip_stage(
                "manual_fixes",
                reason="Manual corrections explicitly disabled.",
                input_paths=[input_csv],
                output_paths=[output_csv],
                input_rows=input_rows,
                output_rows=input_rows,
            )
        return

    if context is not None:
        context.start_stage(
            "manual_fixes", input_paths=[input_csv], output_paths=[output_csv]
        )
    try:
        df, corrected_rows = apply_manual_fixes(input_csv, output_csv)
        unresolved_count = int(
            df.get("needs_manual_review", pd.Series(dtype=bool)).eq(True).sum()
        )
        if context is not None:
            warnings = (
                [f"{unresolved_count} row(s) still require manual review."]
                if unresolved_count
                else []
            )
            context.finish_stage(
                "manual_fixes",
                input_rows=input_rows,
                output_rows=len(df),
                warnings=warnings,
                metrics={
                    "corrections_applied": corrected_rows > 0,
                    "corrected_row_count": corrected_rows,
                    "manual_review_fully_resolved": unresolved_count == 0,
                    "review_count": unresolved_count,
                },
            )
    except Exception as exc:
        if context is not None:
            context.fail_stage("manual_fixes", exc)
        raise


if __name__ == "__main__":
    main()
