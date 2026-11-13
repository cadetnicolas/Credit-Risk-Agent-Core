"""Human feature decisions bind the exact recommendation; no result mutation."""
import time


def recommendation(topic):
    for task in reversed(topic.task_dag):
        if task.plugin_name == "modeling_analysis" and task.plugin_params.get("step") == "feature_select":
            result = topic.task_results.get(task.task_id)
            if result and result.status == "success":
                return task, result, result.output.get("payload") or {}
    raise ValueError("当前口径尚无特征筛选结果")


def validate_features(payload, values):
    selected = (values or {}).get("selected_features")
    allowed = payload.get("selected") or []
    if not isinstance(selected, list) or not selected or any(not isinstance(v, str) or v not in allowed for v in selected) or len(set(selected)) != len(selected):
        raise ValueError("请明确确认非空的入模特征清单，只能保留本轮推荐特征；其他字段需调整口径后重新筛选")
    return selected


def record_review(topic, values, *, actor="human", reason=""):
    task, result, payload = recommendation(topic)
    selected = validate_features(payload, values)
    topic.context["feature_review"] = {"task_id": task.task_id, "result_version": result.version, "selected_features": selected,
        "recommended_features": list(payload.get("selected") or []), "actor": actor, "reason": reason, "confirmed_at": time.time(), "input_binding": result.input_binding}


def reviewed_features(topic):
    task, result, payload = recommendation(topic)
    review = topic.context.get("feature_review") or {}
    if review.get("task_id") != task.task_id or review.get("result_version") != result.version:
        raise ValueError("训练前需人工确认当前版本的特征筛选与分箱证据，旧版确认不能复用")
    return validate_features(payload, review)
