"""Persist model bundles and generate deployment scoring script."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import joblib

from .config import ScorePolicy
from .encoding import CategoricalEncoder


@dataclass
class ModelBundle:
    algorithm: str
    features: List[str]
    label_column: str
    positive_values: Sequence[Any]
    encoder: Dict[str, Any]
    fill_medians: Dict[str, float]
    scaler: Any
    fold_models: List[Any]
    fold_inference: str = "refit"
    fold_scalers: List[Any] = field(default_factory=list)
    n_folds: int = 1
    woe_model: Optional[Dict[str, Any]] = None
    use_woe_transform: bool = False
    score_policy: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def save(self, directory: Path) -> Dict[str, str]:
        directory.mkdir(parents=True, exist_ok=True)
        bundle_path = directory / "model_bundle.joblib"
        joblib.dump(self, bundle_path)
        meta_path = directory / "model_meta.json"
        meta_payload = {
            "algorithm": self.algorithm,
            "features": self.features,
            "label_column": self.label_column,
            "positive_values": list(self.positive_values),
            "encoder": self.encoder,
            "fill_medians": self.fill_medians,
            "n_folds": self.n_folds,
            "fold_inference": self.fold_inference,
            "use_woe_transform": self.use_woe_transform,
            "woe_model": self.woe_model,
            "score_policy": self.score_policy,
            **self.meta,
        }
        meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        score_path = directory / "score.py"
        score_path.write_text(generate_score_script(self.score_policy or {}), encoding="utf-8")
        return {
            "model_dir": str(directory),
            "bundle_path": str(bundle_path),
            "meta_path": str(meta_path),
            "score_script": str(score_path),
        }


def generate_score_script(score_policy: Dict[str, Any]) -> str:
    sp = score_policy or {}
    base = float(sp.get("base_score", 600))
    pdo = float(sp.get("pdo", 50))
    base_odds = float(sp.get("base_odds", 0.05))
    enabled = bool(sp.get("enabled", True))
    return f'''"""Auto-generated online scoring (prob + PDO score)."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List

import joblib
import numpy as np
import pandas as pd

_DIR = Path(__file__).resolve().parent
_BUNDLE = None
_SCORE_ENABLED = {enabled!r}
_BASE_SCORE = {base}
_PDO = {pdo}
_BASE_ODDS = {base_odds}


def _load():
    global _BUNDLE
    if _BUNDLE is None:
        _BUNDLE = joblib.load(_DIR / "model_bundle.joblib")
    return _BUNDLE


def _encode(df: pd.DataFrame, enc: Dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    maps = enc.get("maps") or {{}}
    for c in enc.get("categorical_columns") or []:
        if c not in out.columns:
            continue
        m = maps.get(c, {{}})
        out[c] = out[c].astype(str).replace("nan", np.nan).map(lambda x: m.get(str(x), 0) if pd.notna(x) else 0)
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    return out


def _apply_woe(df: pd.DataFrame, woe: Dict[str, Any]) -> pd.DataFrame:
    specs = woe.get("feature_specs") or {{}}
    out = pd.DataFrame(index=df.index)
    for ft, spec in specs.items():
        if ft not in df.columns:
            out[ft] = 0.0
            continue
        woe_map = {{str(b["bin"]): b["woe"] for b in (spec.get("bins") or [])}}
        if spec.get("kind") == "categorical":
            out[ft] = df[ft].astype(str).map(lambda x: woe_map.get(x, 0.0))
        else:
            edges = spec.get("bin_edges")
            if not edges:
                out[ft] = 0.0
                continue
            binned = pd.cut(df[ft].astype(float), bins=edges, include_lowest=True, duplicates="drop")
            out[ft] = binned.astype(str).map(lambda x: woe_map.get(x, 0.0))
        out[ft] = pd.to_numeric(out[ft], errors="coerce").fillna(0.0)
    return out


def prob_to_score(prob):
    """Higher score = lower risk; _BASE_ODDS is bad:good odds at _BASE_SCORE."""
    eps = 1e-6
    p = np.clip(np.asarray(prob, dtype=float), eps, 1.0 - eps)
    factor = _PDO / math.log(2)
    offset = _BASE_SCORE + factor * math.log(_BASE_ODDS)
    return offset - factor * np.log(p / (1.0 - p))


def predict_proba(rows: pd.DataFrame) -> np.ndarray:
    b = _load()
    feats: List[str] = list(b.features)
    missing = [f for f in feats if f not in rows.columns]
    if missing:
        raise ValueError("missing required features: " + ", ".join(missing))
    work = _encode(rows, b.encoder)
    for c, med in (b.fill_medians or {{}}).items():
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(med)
    if getattr(b, "use_woe_transform", False) and b.woe_model:
        work = _apply_woe(work, b.woe_model)
    X = work[feats].astype(float).values
    scalers = list(getattr(b, "fold_scalers", None) or [])
    preds = []
    for i, m in enumerate(b.fold_models):
        sc = scalers[i] if i < len(scalers) else b.scaler
        Xi = sc.transform(X) if (b.algorithm == "logistic_regression" and sc is not None) else X
        preds.append(m.predict_proba(Xi)[:, 1])
    if not preds:
        raise RuntimeError("no fold models in bundle")
    if b.fold_inference == "best_fold" and len(preds) == 1:
        return preds[0]
    return np.mean(np.vstack(preds), axis=0)


def predict(rows: pd.DataFrame, threshold: float = 0.5) -> Dict[str, Any]:
    p = predict_proba(rows)
    out: Dict[str, Any] = {{"prob": p.tolist(), "label": (p >= threshold).astype(int).tolist()}}
    if _SCORE_ENABLED:
        out["credit_score"] = prob_to_score(p).tolist()
    return out
'''


def package_from_train_result(
    train_body: Dict[str, Any],
    *,
    export_dir: Path,
    spec_meta: Optional[Dict[str, Any]] = None,
    score_policy: Optional[ScorePolicy] = None,
) -> Dict[str, Any]:
    internal = train_body.get("_export") or {}
    if not internal.get("fold_models"):
        return {"error": "export_missing_models", "message": "训练未产生可导出模型"}
    sp = score_policy or ScorePolicy()
    sp_dict = internal.get("score_policy") or {
        "enabled": sp.enabled,
        "base_score": sp.base_score,
        "pdo": sp.pdo,
        "base_odds": sp.base_odds,
    }
    bundle = ModelBundle(
        algorithm=str(train_body.get("algorithm") or "logistic_regression"),
        features=list(train_body.get("features") or []),
        label_column=str((train_body.get("label") or {}).get("column") or ""),
        positive_values=(train_body.get("label") or {}).get("positive_values") or [1],
        encoder=internal.get("encoder") or {},
        fill_medians=internal.get("fill_medians") or {},
        scaler=internal.get("scaler"),
        fold_models=internal.get("fold_models") or [],
        fold_inference=internal.get("fold_inference") or "refit",
        fold_scalers=list(internal.get("fold_scalers") or []),
        n_folds=int(internal.get("n_folds") or 1),
        woe_model=internal.get("woe_model"),
        use_woe_transform=bool(train_body.get("use_woe_transform")),
        score_policy=sp_dict,
        meta={
            "product": (spec_meta or {}).get("product"),
            "metrics": {
                "test_auc": train_body.get("test_auc"),
                "test_ks": train_body.get("test_ks"),
                "cv_auc_mean": train_body.get("cv_auc_mean"),
            },
        },
    )
    paths = bundle.save(export_dir)
    scorecard_path = None
    if sp.export_scorecard_json and train_body.get("coefficients"):
        from .scoring import scorecard_points_from_lr

        woe_prof = internal.get("woe_model") or train_body.get("woe_model")
        if isinstance(woe_prof, dict) and "features" not in woe_prof:
            specs = woe_prof.get("feature_specs") or woe_prof.get("features") or {}
            woe_prof = {"features": specs}
        card = scorecard_points_from_lr(
            woe_prof or {"features": {}},
            train_body.get("coefficients") or [],
            intercept=train_body.get("intercept"),
            pdo=sp.pdo,
            base_score=sp.base_score,
            base_odds=sp.base_odds,
        )
        scorecard_path = export_dir / "scorecard.json"
        scorecard_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        paths["scorecard_json"] = str(scorecard_path)
    return {
        "status": "ready",
        "artifacts": paths,
        "algorithm": bundle.algorithm,
        "features": bundle.features,
        "n_folds": bundle.n_folds,
        "deployment": {
            "usage": "from score import predict  # 含 prob 与 credit_score",
            "score_module": paths["score_script"],
        },
    }
