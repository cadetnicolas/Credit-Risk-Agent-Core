"""Configuration for modeling jobs — all business choices live here, not in core math."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class FeatureSelectPolicy:
    """Filter + IV + correlation (maps to legacy ``AutoModel.preprocessing`` knobs)."""

    min_iv: float = 0.02
    max_missing_rate: float = 0.95
    primary_value_ratio_limit: float = 0.99
    correlation_threshold: float = 0.85
    max_features: int = 40
    numeric_only: bool = False
    iv_bins: int = 10
    max_feature_psi: Optional[float] = 0.25
    psi_reference_fraction: float = 0.7
    woe_bins: int = 10
    emit_woe_profile: bool = True
    numeric_binning: str = "quantile"  # quantile | chi2(chimerge) | monotonic
    monotonic_direction: str = "increasing"  # bad_rate trend across bins
    min_bin_fraction: float = 0.05
    # ChiMerge 预分箱数：先等频切成这么细，再按卡方逐步合并。
    max_prebins: int = 20


@dataclass(frozen=True)
class TrainPolicy:
    test_size: float = 0.3
    random_state: int = 2019
    algorithms: Sequence[str] = ("logistic_regression", "lightgbm")
    min_rows: int = 50
    min_positive: int = 10
    min_negative: int = 10
    n_folds: int = 5
    cv_aggregation: str = "oof"  # oof | mean_valid_auc
    use_woe_transform: bool = True  # LR 路径：特征转 WOE 后训练
    # None keeps probabilities calibrated to the true bad rate (required for PDO scores).
    class_weight: Optional[str] = None


@dataclass(frozen=True)
class ScorePolicy:
    """PDO score mapping for deployment (legacy Prob2Score)."""

    enabled: bool = True
    base_score: float = 600.0
    pdo: float = 50.0
    base_odds: float = 1 / 20
    export_scorecard_json: bool = True


@dataclass(frozen=True)
class ExportPolicy:
    export_model: bool = True
    generate_online_script: bool = True
    # refit: CV estimates performance, the exported model is refit on all training rows.
    fold_inference: str = "refit"
    include_prob_and_score: bool = True


@dataclass(frozen=True)
class EvalPolicy:
    min_ks_for_monotonic_flag: float = 0.05


@dataclass
class ColumnBinding:
    """Explicit column roles — preferred over heuristics (legacy ``drop_list`` / ``y``)."""

    label: Optional[str] = None
    positive_values: Sequence[Any] = (1,)
    negative_values: Sequence[Any] = field(default_factory=tuple)
    exclude_label_values: Sequence[Any] = field(default_factory=tuple)
    gray_values: Sequence[Any] = field(default_factory=tuple)
    gray_policy: str = "exclude"  # exclude | as_good | as_bad
    id_columns: List[str] = field(default_factory=list)
    exclude_columns: List[str] = field(default_factory=list)
    feature_allowlist: List[str] = field(default_factory=list)
    # Observation-time column (e.g. apply_month): drives OOT split and feature PSI.
    time_column: Optional[str] = None
    allow_label_inference: bool = True
    allow_id_inference: bool = True


@dataclass
class ModelingJobSpec:
    """Full job: column binding + policies. Serializable from planner / plugin_params."""

    columns: ColumnBinding = field(default_factory=ColumnBinding)
    feature_select: FeatureSelectPolicy = field(default_factory=FeatureSelectPolicy)
    train: TrainPolicy = field(default_factory=TrainPolicy)
    export: ExportPolicy = field(default_factory=ExportPolicy)
    score: ScorePolicy = field(default_factory=ScorePolicy)
    eval: EvalPolicy = field(default_factory=EvalPolicy)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Optional[Dict[str, Any]]) -> "ModelingJobSpec":
        if not raw:
            return cls()
        cols = raw.get("columns") or {}
        fs = raw.get("feature_select") or {}
        tr = raw.get("train") or {}
        ex = raw.get("export") or {}
        sc = raw.get("score") or {}
        ev = raw.get("eval") or {}
        return cls(
            columns=ColumnBinding(
                label=cols.get("label") or raw.get("label"),
                positive_values=tuple(cols.get("positive_values", raw.get("positive_values", (1,)))),
                negative_values=tuple(cols.get("negative_values") or ()),
                exclude_label_values=tuple(cols.get("exclude_label_values") or ()),
                gray_values=tuple(cols.get("gray_values") or ()),
                gray_policy=str(cols.get("gray_policy") or "exclude"),
                id_columns=list(cols.get("id_columns") or []),
                exclude_columns=list(cols.get("exclude_columns") or cols.get("drop_list") or []),
                feature_allowlist=list(cols.get("feature_allowlist") or cols.get("keep_list") or []),
                time_column=cols.get("time_column") or None,
                allow_label_inference=bool(cols.get("allow_label_inference", True)),
                allow_id_inference=bool(cols.get("allow_id_inference", True)),
            ),
            feature_select=FeatureSelectPolicy(
                **{k: v for k, v in fs.items() if k in FeatureSelectPolicy.__dataclass_fields__}
            ),
            train=TrainPolicy(
                **{k: v for k, v in tr.items() if k in TrainPolicy.__dataclass_fields__}
            ),
            export=ExportPolicy(
                **{k: v for k, v in ex.items() if k in ExportPolicy.__dataclass_fields__}
            ),
            score=ScorePolicy(**{k: v for k, v in sc.items() if k in ScorePolicy.__dataclass_fields__}),
            eval=EvalPolicy(**{k: v for k, v in ev.items() if k in EvalPolicy.__dataclass_fields__}),
            metadata=dict(raw.get("metadata") or {}),
        )
