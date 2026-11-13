"""Credit modeling toolkit for this Agent (successor to ``auto_model_code/model_train``).

Layers: core math → config/schema → workflows → artifacts → :class:`ModelingAnalyzer`.
Use plugin ``modeling_analysis`` in DAGs; import this package for library calls.
"""

from .analyzer import ModelingAnalyzer
from .artifacts import run_artifact
from .by_segment import run_by_segment
from .config import ColumnBinding, ExportPolicy, FeatureSelectPolicy, ModelingJobSpec, ScorePolicy, TrainPolicy
from .logistic import coefficient_signs, decile_table, fit_logistic, stepwise_logistic, vif_table
from .pipeline import feature_select_artifact, model_eval_artifact, model_train_artifact, sample_profile_artifact
from .report_spec import ReportSpec, build_report
from .schema import SchemaResolutionError, resolve_schema
from .scorecard_job import fit_scorecard, fit_scorecard_by_segment

__all__ = [
    "ModelingAnalyzer",
    "ColumnBinding",
    "FeatureSelectPolicy",
    "ModelingJobSpec",
    "TrainPolicy",
    "ExportPolicy",
    "ScorePolicy",
    "SchemaResolutionError",
    "resolve_schema",
    "run_artifact",
    "feature_select_artifact",
    "model_train_artifact",
    "model_eval_artifact",
    "sample_profile_artifact",
    "fit_logistic",
    "stepwise_logistic",
    "coefficient_signs",
    "vif_table",
    "decile_table",
    "ReportSpec",
    "build_report",
    "run_by_segment",
    "fit_scorecard",
    "fit_scorecard_by_segment",
]
