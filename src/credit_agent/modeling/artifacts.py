"""Map workflow outputs to sandbox JSON (report / validator consume these shapes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import ModelingJobSpec
from .encoding import CategoricalEncoder
from .schema import (
    ResolvedSchema,
    filter_modeling_rows,
    label_probe_payload,
    label_to_binary,
    prepare_label_series,
    resolve_schema,
)
from .scoring import scorecard_points_from_lr
from .workflows.feature_selection import feature_selection_step
from .workflows.training import training_step
from .workflows.woe_profile import woe_profile_step


def _envelope(body: Dict[str, Any], *, artifact: str, spec: ModelingJobSpec) -> Dict[str, Any]:
    body = dict(body)
    body.pop("_export", None)
    body["artifact"] = artifact
    body["toolkit"] = "credit_agent.modeling"
    body["toolkit_version"] = "2"
    if spec.metadata:
        body["metadata"] = dict(spec.metadata)
    body["schema"] = {
        "label": spec.columns.label,
        "exclude_columns": list(spec.columns.exclude_columns),
        "feature_allowlist": list(spec.columns.feature_allowlist),
    }
    return body


def label_probe(df: pd.DataFrame, spec: ModelingJobSpec) -> Dict[str, Any]:
    body = label_probe_payload(df, spec.columns)
    return _envelope(body, artifact="label_probe.json", spec=spec)


def sample_profile(df: pd.DataFrame, spec: ModelingJobSpec) -> Dict[str, Any]:
    try:
        work = filter_modeling_rows(df, spec.columns)
        schema = resolve_schema(work, spec.columns, numeric_only=spec.feature_select.numeric_only)
    except Exception as exc:
        probe = label_probe_payload(df, spec.columns)
        return _envelope(
            {
                "error": "schema_resolution_failed",
                "status": "needs_label_definition",
                "message": str(exc),
                "row_count": len(df),
                "label_candidates": probe.get("label_candidates"),
                "resolved_label": probe.get("resolved_label"),
                "unassigned_values": probe.get("unassigned_values"),
                "configured_columns": probe.get("configured_columns"),
                "suggested_defaults": probe.get("suggested_defaults"),
                "human_input_kind": "modeling_label",
            },
            artifact="sample_profile.json",
            spec=spec,
        )
    if not spec.columns.label:
        spec.columns.label = schema.label
    work = filter_modeling_rows(df, spec.columns)
    y = prepare_label_series(df, spec.columns).dropna()
    n = int(len(y))
    bad = int(y.sum())
    from .eda import enrich_sample_profile

    raw_counts = df[schema.label].value_counts(dropna=False)
    core = {
        "status": "ok",
        "sample_size": n,
        "bad_cnt": bad,
        "good_cnt": n - bad,
        "bad_rate": bad / n if n else None,
        "label_column": schema.label,
        "raw_label_counts": [
            {"value": None if pd.isna(k) else (k.item() if hasattr(k, "item") else k), "count": int(v)}
            for k, v in raw_counts.items()
        ],
        "label_mapping": {
            "positive_values": list(spec.columns.positive_values),
            "negative_values": list(spec.columns.negative_values),
            "exclude_label_values": list(spec.columns.exclude_label_values),
            "gray_values": list(spec.columns.gray_values),
            "gray_policy": spec.columns.gray_policy,
        },
        "row_count": n,
        "raw_row_count": len(df),
        "id_columns": schema.id_columns,
    }
    core = enrich_sample_profile(core, work)
    core["row_count"] = n
    return _envelope(core, artifact="sample_profile.json", spec=spec)


def feature_select(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    *,
    preset_features: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if preset_features:
        spec.columns.feature_allowlist = list(preset_features)
    try:
        body = feature_selection_step(df, spec)
    except Exception as exc:
        body = {"error": "feature_selection_failed", "message": str(exc), "row_count": len(df)}
    return _envelope(body, artifact="feature_select.json", spec=spec)


def model_train(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    features: Optional[List[str]] = None,
    *,
    export_dir: Optional[Path] = None,
    encoding: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        enc = CategoricalEncoder.from_dict(encoding) if encoding else None
        if not features:
            sel = feature_selection_step(df, spec)
            if sel.get("error"):
                return _envelope({**sel, "status": "failed"}, artifact="model_train.json", spec=spec)
            features = sel.get("selected") or []
            if enc is None and sel.get("categorical_encoding"):
                enc = CategoricalEncoder.from_dict(sel["categorical_encoding"])
        body = training_step(df, spec, list(features), encoder=enc, export_dir=export_dir)
        body["status"] = "ok" if "error" not in body else "failed"
    except Exception as exc:
        body = {"error": "training_failed", "message": str(exc), "status": "failed"}
    return _envelope(body, artifact="model_train.json", spec=spec)


def model_package(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    features: Optional[List[str]] = None,
    *,
    export_dir: Path,
    encoding: Optional[Dict[str, Any]] = None,
    prior_train: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Export bundle + ``score.py``; re-trains if prior train payload lacks files."""
    if prior_train and prior_train.get("model_dir") and Path(str(prior_train["model_dir"])).exists():
        body = {
            "status": "ready",
            "model_dir": prior_train.get("model_dir"),
            "model_artifacts": prior_train.get("model_artifacts"),
            "test_auc": prior_train.get("test_auc"),
            "test_ks": prior_train.get("test_ks"),
            "from_prior_train": True,
        }
        return _envelope(body, artifact="model_package.json", spec=spec)
    train_body = model_train(
        df,
        spec,
        features=features,
        export_dir=export_dir,
        encoding=encoding,
    )
    if train_body.get("error") or train_body.get("status") == "failed":
        train_body["artifact"] = "model_package.json"
        return train_body
    body = {
        "status": "ready",
        "model_dir": train_body.get("model_dir"),
        "model_artifacts": train_body.get("model_artifacts"),
        "test_auc": train_body.get("test_auc"),
        "test_ks": train_body.get("test_ks"),
        "cv_auc_mean": train_body.get("cv_auc_mean"),
        "n_folds": (train_body.get("sample") or {}).get("n_folds"),
        "deployment": (train_body.get("model_artifacts") or {}),
    }
    return _envelope(body, artifact="model_package.json", spec=spec)


def woe_profile_artifact(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    features: Optional[List[str]] = None,
    *,
    encoding: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cat_cols = None
    if encoding:
        cat_cols = encoding.get("categorical_columns")
    try:
        body = woe_profile_step(df, spec, features=features, categorical_columns=cat_cols)
    except Exception as exc:
        body = {"error": "woe_profile_failed", "message": str(exc)}
    return _envelope(body, artifact="woe_profile.json", spec=spec)


def scorecard_artifact(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    *,
    features: Optional[List[str]] = None,
    woe_profile_body: Optional[Dict[str, Any]] = None,
    train_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    train = train_body
    if train is None:
        train = model_train(df, spec, features=features)
        train = dict(train)
    if train.get("error") or train.get("status") == "failed":
        train["artifact"] = "scorecard.json"
        return train
    woe = woe_profile_body or train.get("woe_model") or train.get("selected_woe_profile")
    if woe and "features" not in woe and woe.get("feature_specs"):
        woe = {"features": woe["feature_specs"]}
    card = scorecard_points_from_lr(
        woe or {"features": {}},
        train.get("coefficients") or [],
        intercept=train.get("intercept"),
        pdo=spec.score.pdo,
        base_score=spec.score.base_score,
        base_odds=spec.score.base_odds,
    )
    card["algorithm"] = train.get("algorithm")
    card["test_auc"] = train.get("test_auc")
    return _envelope(card, artifact="scorecard.json", spec=spec)


def _usable_prior_train(prior_train: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if isinstance(prior_train, dict) and prior_train.get("status") == "ok" and prior_train.get("algorithm"):
        return prior_train
    return None


def model_eval(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    features: Optional[List[str]] = None,
    *,
    prior_train: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    train = _usable_prior_train(prior_train) or model_train(df, spec, features=features)
    if train.get("error") or train.get("status") == "failed":
        train = dict(train)
        train["artifact"] = "model_eval.json"
        return train
    body = {
        "algorithm": train.get("algorithm"),
        "train_auc": train.get("train_auc"),
        "test_auc": train.get("test_auc"),
        "train_ks": train.get("train_ks"),
        "test_ks": train.get("test_ks"),
        "cv_auc_mean": train.get("cv_auc_mean"),
        "score_psi_train_vs_test": train.get("score_psi_train_vs_test"),
        "monotonic_deciles": train.get("monotonic_deciles"),
        "decile_inversions": train.get("decile_inversions") or [],
        "test_deciles": train.get("test_deciles") or [],
        "split": (train.get("sample") or {}).get("split"),
        "from_prior_train": train is prior_train,
        "row_count": len(df),
    }
    return _envelope(body, artifact="model_eval.json", spec=spec)


def run_artifact(
    df: pd.DataFrame,
    artifact_name: str,
    spec: ModelingJobSpec,
    *,
    features: Optional[List[str]] = None,
    preset_features: Optional[List[str]] = None,
    export_dir: Optional[Path] = None,
    encoding: Optional[Dict[str, Any]] = None,
    prior_train: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    art = artifact_name if artifact_name.endswith(".json") else f"{artifact_name}.json"
    if art == "label_probe.json":
        return label_probe(df, spec)
    if art == "sample_profile.json":
        return sample_profile(df, spec)
    if art == "feature_select.json":
        return feature_select(df, spec, preset_features=preset_features)
    if art == "model_train.json":
        return model_train(df, spec, features=features, export_dir=export_dir, encoding=encoding)
    if art == "model_eval.json":
        return model_eval(df, spec, features=features, prior_train=prior_train)
    if art == "model_package.json":
        if export_dir is None:
            return {"error": "missing_export_dir", "artifact": art}
        return model_package(
            df,
            spec,
            features=features,
            export_dir=export_dir,
            encoding=encoding,
            prior_train=prior_train,
        )
    if art == "woe_profile.json":
        return woe_profile_artifact(df, spec, features=features, encoding=encoding)
    if art == "scorecard.json":
        tb = _usable_prior_train(prior_train)
        tb = tb if tb and tb.get("coefficients") else None
        return scorecard_artifact(df, spec, features=features, train_body=tb)
    return {"error": "unsupported_artifact", "artifact": art}
