"""Repeatable functional smoke check on the public fraud and FICO HELOC files.

These datasets are unsuitable for credit performance-window admission: fraud
spans about two days and HELOC has no decision dates. This check never invents
missing origination dates or treats challenge-set rates as portfolio rates.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from credit_agent.analytics.strategy import marginal_bands, score_grid, swap_table
from credit_agent.modeling.scorecard_job import fit_scorecard
from credit_agent.tools import Ledger, check_label_maturity, inspect_and_bind_snapshot, simulate_policy_rules


def run(fraud_csv: Path, heloc_csv: Path) -> dict:
    heloc = pd.read_csv(heloc_csv)
    heloc = heloc.loc[pd.to_numeric(heloc["ExternalRiskEstimate"], errors="coerce") > 0].copy()
    heloc["bad"] = heloc["RiskPerformance"].eq("Bad").astype(int)
    bands = marginal_bands(heloc, "ExternalRiskEstimate", "bad", edges=[0, 50, 60, 70, 80, 100])
    grid = score_grid(heloc, "ExternalRiskEstimate", "NumInqLast6M", "bad",
                      edges_a=[0, 60, 70, 80, 100], edges_b=[-1, 0, 1, 2, 4, 100])
    swap = swap_table(heloc, "ExternalRiskEstimate", "bad", current_threshold=70,
                      candidate_threshold=60)
    assert bands["n"] == len(heloc)
    assert grid["n"] > 0 and swap["swap_in"]["n"] > 0

    with tempfile.TemporaryDirectory(prefix="credit-public-verify-") as directory:
        ledger = Ledger(Path(directory))
        # HELOC has no decision date; no admission or maturity record is invented.
        ledger.save_frame("heloc", heloc)
        policy_gate = simulate_policy_rules("heloc", ledger=ledger, mode="cutoff", time_column=None,
                                            bad="bad", score="ExternalRiskEstimate",
                                            current_threshold=70, candidate_threshold=60)
        assert policy_gate["block_code"] == "ADMISSION_REQUIRED"

        fraud = pd.read_csv(fraud_csv)
        fraud = fraud.sort_values("Time", kind="stable").iloc[::8].copy().reset_index(drop=True)
        fraud["txn_id"] = np.arange(len(fraud)).astype(str)
        # Only relative transaction order is used by the scorecard's OOT split.
        fraud["event_time"] = pd.Timestamp("2013-09-01") + pd.to_timedelta(fraud["Time"], unit="s")
        model = fit_scorecard(fraud, label="Class", features=["V14", "V4", "V12"],
                              time_column="event_time", id_column="txn_id", max_features=3,
                              report_metrics=["auc", "ks", "bad_rate", "lift", "psi", "decile_bad_rate"])
        assert model["split_method"] == "oot"
        assert model["holdout_report"]["auc"] is not None

        fraud["cohort"] = fraud["event_time"].dt.strftime("%Y-%m")
        admitted = inspect_and_bind_snapshot(fraud, ledger=ledger, grain="application",
                                             key_columns=["txn_id"], decision_time_column="event_time",
                                             observation_time_column="event_time")
        maturity = check_label_maturity(admitted["snapshot_id"], ledger=ledger,
                                        cohort_column="cohort", decision_time_column="event_time",
                                        performance_window={"kind": "mob", "mob": 3},
                                        bad_flag_column="Class")
        assert maturity["block_code"] == "LABEL_IMMATURE"

    return {
        "heloc_rows": int(len(heloc)),
        "heloc_observed_bad_rate": float(heloc["bad"].mean()),
        "heloc_bands": len(bands["bands"]),
        "heloc_grid_cells": len(grid["cells"]),
        "heloc_research_swap_in": swap["swap_in"]["n"],
        "heloc_policy_gate": policy_gate["block_code"],
        "fraud_rows": int(len(fraud)),
        "fraud_positive_rows": int(fraud["Class"].sum()),
        "fraud_scorecard_split": model["split_method"],
        "fraud_selected_features": model["selected"],
        "fraud_holdout_auc": model["holdout_report"]["auc"],
        "fraud_holdout_ks": model["holdout_report"]["ks"],
        "fraud_credit_maturity": maturity["block_code"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fraud", type=Path, required=True)
    parser.add_argument("--heloc", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.fraud, args.heloc), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
