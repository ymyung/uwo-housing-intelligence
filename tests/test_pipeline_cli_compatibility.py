from pathlib import Path

from pipeline.uwo_listing_enricher import build_parser as build_stage1_parser
from scraper.collect_listing_urls import build_parser as build_stage0_parser


def test_stage0_legacy_output_cli_remains_available() -> None:
    args = build_stage0_parser().parse_args(["--output", "legacy-links.csv"])

    assert args.output == Path("legacy-links.csv")
    assert args.run_dir is None


def test_stage0_run_directory_cli_is_available() -> None:
    args = build_stage0_parser().parse_args(
        ["--run-dir", "data/runs/example", "--max-pages", "1"]
    )

    assert args.run_dir == Path("data/runs/example")
    assert args.max_pages == 1


def test_stage1_legacy_positional_cli_remains_available() -> None:
    args = build_stage1_parser().parse_args(
        [
            "legacy-links.csv",
            "--output-csv",
            "legacy-details.csv",
            "--checkpoint-csv",
            "legacy-checkpoint.csv",
        ]
    )

    assert args.input_csv == Path("legacy-links.csv")
    assert args.output_csv == Path("legacy-details.csv")
    assert args.checkpoint_csv == Path("legacy-checkpoint.csv")
    assert args.run_dir is None


def test_stage1_run_directory_does_not_require_positional_input() -> None:
    args = build_stage1_parser().parse_args(
        ["--run-dir", "data/runs/example", "--limit", "5"]
    )

    assert args.input_csv is None
    assert args.run_dir == Path("data/runs/example")
    assert args.limit == 5
