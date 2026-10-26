"""拍摄收场沿用直接产生失败的原准备结果，不查询历史补造诊断。"""

from camctl.logging_runtime.diagnostics import DiagnosticState


def preparation_failure_diagnostic(runtime, action_id, failure):
    """已记录的目录或设置失败与其动作收场共用原状态。"""
    if failure is None or failure.code != "capture_failed":
        return None
    reason = failure.details.get("reason")
    owner = None
    if reason == "baseline_read_failed":
        owner = runtime.pending_baselines.get(action_id)
    elif reason == "device_failed" and runtime.preparation_cache is not None:
        owner = runtime.preparation_cache.owners.get(action_id)
    diagnostic = getattr(owner, "diagnostic", None)
    return diagnostic if isinstance(diagnostic, DiagnosticState) else None
