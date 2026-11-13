"""Training workflow: dev/test split first, leak-free K-fold CV, final refit, export.

Pipeline per fit (fold-train, or the whole development set for the final model):
categorical encoder -> median fill -> optional WOE -> estimator. Every step is
fitted on the rows it trains on only, then applied to the held-out rows.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold

from ..config import ColumnBinding, ExportPolicy, ModelingJobSpec, ScorePolicy, TrainPolicy
from ..encoding import CategoricalEncoder
from ..export import package_from_train_result
from ..metrics import auc_score, decile_bad_rates, ks_score, psi
from ..schema import ResolvedSchema, binary_label_series, filter_modeling_rows
from ..split import dev_test_split
from ..woe import WoEBinningModel


@dataclass
class FittedPipeline:
    algorithm: str
    features: List[str]
    encoder: CategoricalEncoder
    fill_medians: Dict[str, float]
    woe_model: Optional[WoEBinningModel]
    model: Any

    def matrix(self, frame: pd.DataFrame) -> np.ndarray:
        work = self.encoder.transform(frame[self.features])
        if self.woe_model is not None:
            # WOE 路径不能再填中位数：填了以后缺失就落进中间箱，缺失箱永远不可达。
            return self.woe_model.transform(work)[self.features].astype(float).values
        for c, med in self.fill_medians.items():
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(med)
        return work[self.features].astype(float).values

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self.matrix(frame))[:, 1]


def _medians(df: pd.DataFrame, feats: Sequence[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for c in feats:
        s = pd.to_numeric(df[c], errors="coerce")
        out[c] = float(s.median()) if s.notna().any() else 0.0
    return out


def _new_lgbm(random_state: int):
    try:
        from lightgbm import LGBMClassifier
    except (ImportError, OSError):
        return None
    return LGBMClassifier(
        n_estimators=300,
        num_leaves=24,
        learning_rate=0.05,
        max_depth=5,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        min_child_samples=20,
        random_state=random_state,
        verbosity=-1,
        n_jobs=2,
    )


def _fit_pipeline(
    frame: pd.DataFrame,
    y: np.ndarray,
    feats: List[str],
    algorithm: str,
    policy: TrainPolicy,
    *,
    encoder: Optional[CategoricalEncoder],
    woe_kwargs: Dict[str, Any],
) -> Optional[FittedPipeline]:
    enc = CategoricalEncoder().fit(
        frame, feats, categorical_columns=encoder.categorical_columns if encoder is not None else None
    )
    work = enc.transform(frame[feats])
    woe_model: Optional[WoEBinningModel] = None
    if algorithm == "logistic_regression" and policy.use_woe_transform:
        # WOE 分箱必须在「原始缺失」上做。此前先 fillna(median) 再拟合 WOE，
        # 导致缺失被并进中间箱、缺失独立箱永远建不出来，缺失本身承载的信号被抹掉；
        # 同一个根因还会让相关系数被稀释（大量样本压成同一个中位数），共线性剔不掉。
        work = work.assign(_y=y)
        woe_model = WoEBinningModel().fit(
            work, feats, "_y", categorical_columns=enc.categorical_columns, **woe_kwargs
        )
        X = woe_model.transform(work)[feats].astype(float).values
        medians: Dict[str, float] = {}
    else:
        medians = _medians(work, feats)
        for c, med in medians.items():
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(med)
        X = work[feats].astype(float).values
    if algorithm == "logistic_regression":
        model = LogisticRegression(max_iter=2000, class_weight=policy.class_weight)
    elif algorithm == "lightgbm":
        model = _new_lgbm(policy.random_state)
        if model is None:
            return None
    else:
        return None
    model.fit(X, y)
    return FittedPipeline(algorithm, list(feats), enc, medians, woe_model, model)


def _metrics(y: np.ndarray, p: np.ndarray) -> Dict[str, Optional[float]]:
    auc = auc_score(y, p)
    ks, _ = ks_score(y, p)
    return {
        "auc": float(auc) if np.isfinite(auc) else None,
        "ks": float(ks) if np.isfinite(ks) else None,
    }


def train_classifier(
    df: pd.DataFrame,
    schema: ResolvedSchema,
    features: Sequence[str],
    policy: TrainPolicy,
    export_policy: ExportPolicy,
    score_policy: ScorePolicy,
    *,
    encoder: Optional[CategoricalEncoder] = None,
    export_dir: Optional[Path] = None,
    spec_meta: Optional[Dict[str, Any]] = None,
    woe_bins: int = 10,
    binning_method: str = "quantile",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
    columns: Optional[ColumnBinding] = None,
) -> Dict[str, Any]:
    feats = [f for f in features if f in schema.feature_columns]
    if not feats:
        return {"error": "no_features", "features": []}

    base = filter_modeling_rows(df, columns) if columns else df
    keep_cols = list(dict.fromkeys(feats + [schema.label] + ([columns.time_column] if columns and columns.time_column and columns.time_column in base.columns else []) + ([columns.id_columns[0]] if columns and columns.id_columns else [])))
    work = base[keep_cols].copy()
    if columns:
        work["_y"] = binary_label_series(work, columns)
    else:
        from ..schema import label_to_binary

        work["_y"] = label_to_binary(work[schema.label], schema.label_positive_values)
    work = work.dropna(subset=["_y"]).reset_index(drop=True)
    y_all = work["_y"].values.astype(int)

    pos = int(y_all.sum())
    neg = int(len(y_all) - pos)
    if len(work) < policy.min_rows:
        return {"error": "insufficient_rows", "row_count": int(len(work)), "min_rows": policy.min_rows}
    if pos < policy.min_positive or neg < policy.min_negative:
        return {"error": "insufficient_class_balance", "pos_cnt": pos, "neg_cnt": neg}

    split = dev_test_split(
        work,
        y_all,
        test_size=policy.test_size,
        random_state=policy.random_state,
        time_column=columns.time_column if columns else None,
        group_column=columns.id_columns[0] if columns and columns.id_columns else None,
    )
    dev = work[split.dev_mask].reset_index(drop=True)
    test = work[split.test_mask].reset_index(drop=True)
    y_dev = dev["_y"].values.astype(int)
    y_test = test["_y"].values.astype(int)

    woe_kwargs = {
        "n_bins": woe_bins,
        "binning_method": binning_method,
        "monotonic_direction": monotonic_direction,
        "min_bin_fraction": min_bin_fraction,
        "max_prebins": max_prebins,
    }
    algorithms = [a for a in policy.algorithms if a in ("logistic_regression", "lightgbm")]
    n_folds = max(1, int(policy.n_folds))
    use_cv = n_folds > 1 and len(dev) >= n_folds * 20 and min(np.bincount(y_dev, minlength=2)) >= n_folds
    if split.method == "cv_only" and not use_cv:
        return {"error": "cv_unavailable", "message": "样本量或正负样本不足，无法进行交叉验证"}

    models_meta: Dict[str, Any] = {}
    oof_by_algo: Dict[str, np.ndarray] = {}
    if use_cv:
        skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=policy.random_state)
        if columns and columns.id_columns and dev[columns.id_columns[0]].duplicated().any():
            from sklearn.model_selection import StratifiedGroupKFold
            groups = dev[columns.id_columns[0]].astype(str).to_numpy()
            splits = list(StratifiedGroupKFold(n_splits=n_folds, shuffle=True, random_state=policy.random_state).split(dev, y_dev, groups))
        else:
            splits = list(skf.split(np.zeros(len(dev)), y_dev))
        if any(len(np.unique(y_dev[a])) != 2 or len(np.unique(y_dev[b])) != 2 for a, b in splits):
            return {"error": "insufficient_cv_classes", "message": "实体隔离的交叉验证折缺少好或坏样本，请补充样本"}
        for algo in algorithms:
            oof = np.full(len(dev), np.nan)
            fold_aucs: List[float] = []
            for tr_idx, va_idx in splits:
                pipe = _fit_pipeline(
                    dev.iloc[tr_idx], y_dev[tr_idx], feats, algo, policy, encoder=encoder, woe_kwargs=woe_kwargs
                )
                if pipe is None:
                    break
                p = pipe.predict(dev.iloc[va_idx])
                oof[va_idx] = p
                fold_aucs.append(auc_score(y_dev[va_idx], p))
            if not fold_aucs:
                continue
            oof_by_algo[algo] = oof
            ok = np.isfinite(oof)
            m = _metrics(y_dev[ok], oof[ok])
            models_meta[algo] = {
                "cv_auc_mean": float(np.nanmean(fold_aucs)),
                "cv_auc_std": float(np.nanstd(fold_aucs)),
                "oof_auc": m["auc"],
                "oof_ks": m["ks"],
                "n_folds": n_folds,
            }
        selection_rule = f"development {n_folds}-fold CV mean AUC"
    selection_scores = {a: (models_meta.get(a) or {}).get("cv_auc_mean") for a in algorithms}

    finals: Dict[str, FittedPipeline] = {}
    for algo in algorithms:
        pipe = _fit_pipeline(dev, y_dev, feats, algo, policy, encoder=encoder, woe_kwargs=woe_kwargs)
        if pipe is None:
            continue
        finals[algo] = pipe
        p_dev = pipe.predict(dev)
        p_test = pipe.predict(test) if len(test) else np.array([])
        m_dev = _metrics(y_dev, p_dev)
        m_test = _metrics(y_test, p_test) if len(test) else {"auc": None, "ks": None}
        models_meta.setdefault(algo, {}).update(
            {"train_auc": m_dev["auc"], "train_ks": m_dev["ks"], "test_auc": m_test["auc"], "test_ks": m_test["ks"]}
        )
        if not use_cv:
            selection_scores[algo] = m_dev["auc"]
    if not finals:
        return {"error": "no_trainable_algorithm", "algorithms": list(policy.algorithms)}
    if "logistic_regression" in algorithms and "logistic_regression" not in finals:
        return {"error": "baseline_unavailable", "algorithms": list(policy.algorithms)}
    if not use_cv:
        selection_rule = "development AUC (no CV: insufficient rows)"

    baseline_card = None
    baseline = finals.get("logistic_regression")
    if baseline is not None and baseline.woe_model is not None:
        from ..scoring import scorecard_points_from_lr

        coefficients = [
            {"feature": feature, "coef": float(coef)}
            for feature, coef in zip(feats, baseline.model.coef_[0])
        ]
        baseline_card = scorecard_points_from_lr(
            baseline.woe_model.to_dict(), coefficients,
            intercept=float(baseline.model.intercept_[0]),
            pdo=score_policy.pdo,
            base_score=score_policy.base_score,
            base_odds=score_policy.base_odds,
        )

    best_name = max(
        finals.keys(),
        key=lambda a: (selection_scores.get(a) if selection_scores.get(a) is not None else -1.0),
    )
    best = finals[best_name]
    p_dev = best.predict(dev)
    p_test = best.predict(test) if len(test) else np.array([])
    meta = models_meta[best_name]
    score_psi = psi(p_dev, p_test) if len(p_test) else float("nan")
    deciles = decile_bad_rates(y_test, p_test) if len(p_test) else {"rows": [], "monotonic": None}

    coef: List[Dict[str, Any]] = []
    intercept: Optional[float] = None
    if best_name == "logistic_regression":
        lr = best.model
        coef = [
            {"feature": ft, "coef": float(c)}
            for ft, c in sorted(zip(feats, lr.coef_[0]), key=lambda x: -abs(x[1]))
        ]
        intercept = float(lr.intercept_[0])

    woe_dict = best.woe_model.to_dict() if best.woe_model else None
    body: Dict[str, Any] = {
        "algorithm": best_name,
        "unavailable_algorithms": [name for name in algorithms if name not in finals],
        "baseline_scorecard": baseline_card,
        "features": list(feats),
        "train_size": int(len(dev)),
        "test_size": int(len(test)),
        "train_auc": meta.get("train_auc"),
        "test_auc": meta.get("test_auc"),
        "train_ks": meta.get("train_ks"),
        "test_ks": meta.get("test_ks"),
        "cv_auc_mean": meta.get("cv_auc_mean"),
        "cv_auc_std": meta.get("cv_auc_std"),
        "coefficients": coef,
        "intercept": intercept,
        "models": models_meta,
        "best_model": {
            "name": best_name,
            "selection_rule": selection_rule,
            "valid_auc": meta.get("cv_auc_mean") if use_cv else meta.get("train_auc"),
            "test_auc": meta.get("test_auc"),
        },
        "sample": {
            "model_rows": int(len(work)),
            "train_rows": int(len(dev)),
            "valid_rows": int(len(test)),
            "split": split.description,
            "split_method": split.method,
            "n_folds": n_folds if use_cv else 1,
            "group_column": columns.id_columns[0] if columns and columns.id_columns else None,
            "purged_rows": int((~(split.dev_mask | split.test_mask)).sum()),
            "train_bad": int(y_dev.sum()), "test_bad": int(y_test.sum()),
            "fit_scope": "encoder / medians / WOE fitted on training rows only",
        },
        "split_spec": {
            "test_size": policy.test_size, "random_state": policy.random_state,
            "time_column": columns.time_column if columns else None,
            "group_column": columns.id_columns[0] if columns and columns.id_columns else None,
            "test_positions_digest": hashlib.sha256(np.flatnonzero(split.test_mask).astype('<i8').tobytes()).hexdigest(),
        },
        "label": {
            "column": schema.label,
            "pos_cnt": pos,
            "positive_ratio": float(y_all.mean()),
            "positive_values": list(schema.label_positive_values),
        },
        "feature_summary": {"features": list(feats), "n_after_filter": len(feats)},
        "categorical_encoding": best.encoder.to_dict(),
        "use_woe_transform": best.woe_model is not None,
        "woe_model": woe_dict,
        "binning_policy": {
            "method": binning_method,
            "max_bins": int(woe_bins),
            "min_bin_fraction": float(min_bin_fraction),
            "min_bin_fraction_scope": "valid_only",
            "max_prebins": int(max_prebins),
            "missing_bin": "MISSING",
        },
        "score_psi_train_vs_test": float(score_psi) if np.isfinite(score_psi) else None,
        "test_deciles": deciles["rows"],
        "monotonic_deciles": deciles["monotonic"],
        "decile_inversions": deciles.get("inversions") or [],
        "_export": {
            "fold_models": [best.model],
            "scaler": None,
            "encoder": best.encoder.to_dict(),
            "woe_model": woe_dict,
            "fill_medians": best.fill_medians,
            "n_folds": 1,
            "fold_inference": "refit",
            "score_policy": {
                "enabled": score_policy.enabled,
                "base_score": score_policy.base_score,
                "pdo": score_policy.pdo,
                "base_odds": score_policy.base_odds,
            },
        },
    }

    # Curves expose aggregate rates only; individual scores stay in the research runtime.
    from sklearn.metrics import roc_curve
    if len(test) and len(np.unique(y_test)) == 2:
        fpr, tpr, _ = roc_curve(y_test, p_test, drop_intermediate=True)
        indices = np.unique(np.linspace(0, len(fpr)-1, min(len(fpr), 200), dtype=int))
        body["roc_curve"] = [{"x": float(fpr[i]), "y": float(tpr[i])} for i in indices]

    if export_policy.export_model and export_dir is not None:
        pkg = package_from_train_result(
            body,
            export_dir=export_dir,
            spec_meta=spec_meta,
            score_policy=score_policy,
        )
        body["model_artifacts"] = pkg.get("artifacts")
        body["model_dir"] = (pkg.get("artifacts") or {}).get("model_dir")
        if pkg.get("error"):
            body["export_error"] = pkg

    return body


def training_step(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    features: List[str],
    *,
    encoder: Optional[CategoricalEncoder] = None,
    export_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    from ..schema import resolve_schema

    work = filter_modeling_rows(df, spec.columns)
    schema = resolve_schema(work, spec.columns, numeric_only=spec.feature_select.numeric_only)
    if not spec.columns.label:
        spec.columns.label = schema.label
    return train_classifier(
        df,
        schema,
        features,
        spec.train,
        spec.export,
        spec.score,
        encoder=encoder,
        export_dir=export_dir,
        spec_meta=spec.metadata,
        woe_bins=spec.feature_select.woe_bins,
        binning_method=spec.feature_select.numeric_binning,
        monotonic_direction=spec.feature_select.monotonic_direction,
        min_bin_fraction=spec.feature_select.min_bin_fraction,
        max_prebins=spec.feature_select.max_prebins,
        columns=spec.columns,
    )
