"""Create a synthetic demo workspace so the MCP tools can be exercised end to end.

This writes ONLY into a workspace directory (default ``~/credit-agent-workspace``)
and never touches real business data. The generated files are clearly named
``demo_*`` so they cannot be mistaken for production datasets.

Usage::

    python scripts/make_demo_workspace.py
    python scripts/make_demo_workspace.py --workspace /tmp/demo-ws --loans 300

Output layout (matches the three disjoint roots the MCP server requires)::

    <workspace>/datasets/demo_loan_panel.csv
    <workspace>/registries/registry_demo.json
    <workspace>/ledger/                     (left empty; the server creates it)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_WORKSPACE = Path.home() / "credit-agent-workspace"

# Six disbursement cohorts. The last two are intentionally left immature so the
# maturity tool has something interesting to report.
COHORTS = [f"2025-{month:02d}" for month in range(10, 13)] + [
    f"2026-{month:02d}" for month in range(1, 4)
]
# Risk deterioration over time: later cohorts are worse.
COHORT_BAD_RATE = {
    "2025-10": 0.055,
    "2025-11": 0.062,
    "2025-12": 0.074,
    "2026-01": 0.096,
    "2026-02": 0.128,
    "2026-03": 0.161,
}
CHANNEL_MIX = {"online": 0.62, "branch": 0.38}
CHANNEL_BAD_MULTIPLIER = {"online": 1.28, "branch": 0.63}
REGION_BAD_MULTIPLIER = {"east": 0.92, "south": 1.05, "north": 1.18}


def _observation_day(cohort: str, months: int) -> pd.Timestamp:
    """First day of the month that is ``months`` full MOBs after the cohort."""

    year, month = (int(part) for part in cohort.split("-"))
    total = year * 12 + (month - 1) + months
    return pd.Timestamp(year=total // 12, month=total % 12 + 1, day=1)


def build_panel(loans_per_cohort: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    channels = list(CHANNEL_MIX)
    channel_p = list(CHANNEL_MIX.values())
    regions = list(REGION_BAD_MULTIPLIER)
    for cohort_index, cohort in enumerate(COHORTS):
        base = COHORT_BAD_RATE[cohort]
        for index in range(loans_per_cohort):
            channel = str(rng.choice(channels, p=channel_p))
            region = str(rng.choice(regions))
            probability = base * CHANNEL_BAD_MULTIPLIER[channel] * REGION_BAD_MULTIPLIER[region]
            probability = float(min(max(probability, 0.002), 0.95))
            bad = int(rng.random() < probability)
            # Score is a monotone-ish risk signal: higher score = worse.
            score = float(rng.normal(bad * 2.4 + probability * 3.0, 1.15))
            balance = float(np.round(rng.lognormal(mean=9.6, sigma=0.55), 2))
            decision = pd.Timestamp(year=int(cohort[:4]), month=int(cohort[5:]), day=10)
            rows.append(
                {
                    "loan_id": f"{cohort}-{index:04d}",
                    "cohort": cohort,
                    "decision_time": decision,
                    "period": cohort,
                    "bad": bad,
                    "channel": channel,
                    "region": region,
                    "score": round(score, 4),
                    "balance": balance,
                    # 3 complete MOBs of performance for every cohort.
                    "obs_time": _observation_day(cohort, 3),
                    # ``mob`` is what the vintage view needs to align cohorts on
                    # comparable months-on-book; without it the view is skipped.
                    "mob": 3,
                    "cohort_index": cohort_index,
                }
            )
    frame = pd.DataFrame(rows)
    return frame.sort_values(["cohort", "loan_id"]).reset_index(drop=True)


def build_registry(panel: pd.DataFrame) -> dict:
    return {
        "registry_version": "demo-1",
        "owner": "demo",
        "note": (
            "Synthetic demo registry. Not a signed business artifact. "
            "Replace with the risk-management signed registry before real use."
        ),
        "metrics": [
            {
                "metric_id": "bad_rate",
                "version": "v1",
                "name_zh": "不良率",
                "flag_column": "bad",
                "period_column": "period",
                # Column roles are declared here, never guessed by the tool.
                # Declaring these three turns the vintage view from "skipped,
                # missing columns" into an actual cohort curve.
                "loan_id": "loan_id",
                "cohort": "cohort",
                "mob": "mob",
            }
        ],
        "dimensions": [
            {
                "column": "channel",
                "point_in_time": "decision",
                "max_cardinality": 12,
                "min_cell_n": 50,
                "min_expected_bad": 5,
            },
            {
                "column": "region",
                "point_in_time": "decision",
                "max_cardinality": 12,
                "min_cell_n": 50,
                "min_expected_bad": 5,
            },
            {
                # Deliberately NOT registered as a decision dimension: ``cohort``
                # defines the period itself, so decomposing by it only ever
                # produces a pure mix effect. Keeping it out is the correct
                # example to copy.
                "column": "cohort",
                "point_in_time": "observation",
            },
            {
                # Explicitly declared as an outcome column so the tool can prove
                # it is NOT offered as a decision-time dimension.
                "column": "obs_time",
                "point_in_time": "observation",
            },
        ],
        "columns": list(panel.columns),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--loans", type=int, default=200, help="loans per cohort")
    parser.add_argument("--seed", type=int, default=20261112)
    parser.add_argument("--force", action="store_true", help="overwrite existing demo files")
    options = parser.parse_args(argv)

    if options.loans < 20:
        parser.error("--loans must be at least 20")

    datasets = options.workspace / "datasets"
    registries = options.workspace / "registries"
    ledger = options.workspace / "ledger"
    for directory in (datasets, registries, ledger):
        directory.mkdir(parents=True, exist_ok=True)

    csv_path = datasets / "demo_loan_panel.csv"
    registry_path = registries / "registry_demo.json"
    for path in (csv_path, registry_path):
        if path.exists() and not options.force:
            print(f"FAIL: {path} already exists; pass --force to overwrite", file=sys.stderr)
            return 2

    panel = build_panel(options.loans, options.seed)
    panel.to_csv(csv_path, index=False)
    registry = build_registry(panel)
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    overall = panel["bad"].mean()
    print(f"OK: wrote {csv_path} ({len(panel)} rows, {panel['bad'].sum()} bad, "
          f"bad_rate={overall:.4f})")
    print(f"OK: wrote {registry_path} (metric bad_rate/v1, "
          f"{len(registry['dimensions'])} declared dimensions)")
    print(f"OK: ledger directory ready at {ledger}")
    print()
    print("Cohort bad rates (for eyeballing the demo):")
    rates = panel.groupby("cohort")["bad"].agg(["count", "sum", "mean"])
    for cohort, row in rates.iterrows():
        print(f"  {cohort}  n={int(row['count']):4d}  bad={int(row['sum']):3d}  "
              f"rate={row['mean']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
