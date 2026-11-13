"""In-process modeling API for Agent plugins (replaces legacy ``AutoModel`` orchestration)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .artifacts import run_artifact
from .config import ModelingJobSpec


class ModelingAnalyzer:
    """Run sample profile → feature selection → train → eval on a dataframe."""

    SUPPORTED_ARTIFACTS = frozenset(
        {
            "label_probe.json",
            "sample_profile.json",
            "feature_select.json",
            "model_train.json",
            "model_eval.json",
            "model_package.json",
            "woe_profile.json",
            "scorecard.json",
        }
    )

    def __init__(self, spec: Optional[ModelingJobSpec] = None) -> None:
        self.spec = spec or ModelingJobSpec()

    @classmethod
    def from_params(
        cls,
        params: Dict[str, Any],
        *,
        product: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> "ModelingAnalyzer":
        raw: Dict[str, Any] = {}
        block = params.get("modeling")
        if isinstance(block, dict):
            raw = dict(block)
        cols = dict(raw.get("columns") or {})
        # Agent jobs must supply business semantics; the low-level toolkit's
        # historical binary default is not a confirmed label definition.
        cols.setdefault("positive_values", [])
        ctx_label = (context or {}).get("modeling_label_spec")
        if isinstance(ctx_label, dict):
            for key in (
                "label",
                "positive_values",
                "negative_values",
                "exclude_label_values",
                "gray_values",
                "gray_policy",
                "time_column",
            ):
                if ctx_label.get(key) is not None:
                    cols[key] = ctx_label[key]
        confirmed = ctx_label.get("label") if isinstance(ctx_label, dict) else None
        label = confirmed or params.get("label_column") or cols.get("label")
        if label:
            cols["label"] = str(label)
        if cols:
            raw["columns"] = cols
        contract = (context or {}).get("modeling_contract") or {}
        if contract.get("research_mode"):
            cols["time_column"] = contract.get("time_column") or None
            cols["id_columns"] = [contract["entity_id_column"]] if contract.get("entity_id_column") else []
            cols["exclude_columns"] = sorted(set(cols.get("exclude_columns") or []) | set(contract.get("exclude_columns") or []))
            raw["columns"] = cols
            # The final holdout may not select features or algorithms.
            raw["feature_select"] = {**(raw.get("feature_select") or {}), "max_feature_psi": None}
        meta = dict(raw.get("metadata") or {})
        if contract:
            meta["modeling_contract"] = contract
        if product:
            meta.setdefault("product", product)
        if meta:
            raw["metadata"] = meta
        return cls(ModelingJobSpec.from_mapping(raw))

    def run(
        self,
        df: pd.DataFrame,
        artifact_name: str,
        *,
        features: Optional[List[str]] = None,
        preset_features: Optional[List[str]] = None,
        export_dir: Optional[Path] = None,
        encoding: Optional[Dict[str, Any]] = None,
        prior_train: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        art = artifact_name if artifact_name.endswith(".json") else f"{artifact_name}.json"
        if art not in self.SUPPORTED_ARTIFACTS:
            return {"error": "unsupported_artifact", "artifact": art}
        return run_artifact(
            df,
            art,
            self.spec,
            features=features,
            preset_features=preset_features,
            export_dir=export_dir,
            encoding=encoding,
            prior_train=prior_train,
        )
