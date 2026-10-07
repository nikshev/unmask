# impl: FR-005-01
"""CLI для бенчмарку 005."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from unmask.benchmark.ground_truth import load_ground_truth
from unmask.benchmark.runner import run_benchmark
from unmask.benchmark.report import generate_report

__all__ = ["main"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unmask benchmark: оцінювання детектора фінансової координації за ground truth."
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        required=True,
        help="Шлях до ground_truth.jsonl (v1).",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("tests/fixtures/real"),
        help="Директорія з фікстурами (offline режим).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Живий режим: збір через RPC (потребує UNMASK_RPC_URL).",
    )
    parser.add_argument(
        "--mints",
        type=str,
        help="CSV список мінтів для live режиму.",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Запустити калібрування порогів band.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Шлях для збереження Markdown звіту.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Детальний вивід.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    gt = load_ground_truth(args.ground_truth)
    print(f"Loaded {len(gt)} ground truth records", file=sys.stderr)

    if args.calibrate:
        print("Calibration not fully implemented yet", file=sys.stderr)
        return 1

    if args.live:
        if not args.mints:
            print("Error: --mints required for --live", file=sys.stderr)
            return 1
        mints = [m.strip() for m in args.mints.split(",")]
        gt = [{"mint": m, "class_": "unknown", "source": "live"} for m in mints]

    fixtures_dir = args.fixtures
    print(f"Running benchmark on {len(gt)} tokens...", file=sys.stderr)

    preds = run_benchmark(gt, fixtures_dir, live=args.live)

    # Config versions
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[3]
    ingest_cfg = yaml.safe_load((root / "config" / "ingest.yaml").read_text())
    hubs_cfg = yaml.safe_load((root / "config" / "hubs.yaml").read_text())
    hub_lists_cfg = yaml.safe_load((root / "config" / "hub_addresses.yaml").read_text())
    clusters_cfg = yaml.safe_load((root / "config" / "clusters.yaml").read_text())

    config_versions = {
        "ingest": ingest_cfg.get("version"),
        "hubs": hubs_cfg.get("thresholds", {}).get("version"),
        "hub_addresses": hub_lists_cfg.get("version"),
        "clusters": clusters_cfg.get("version"),
    }

    # Generate report
    report = generate_report(
        predictions=preds,
        ground_truth=list(gt),
        config_versions=config_versions,
        latency_stats={},
        calibrated_thresholds={},
    )

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())