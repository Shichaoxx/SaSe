"""Describe native performance on one pinned pagination calibration batch."""

from collections import Counter

from .evaluation import TASK_TYPES, score


METRICS = ("tool_use", "natural_stop", "strict_json", "strict_success")
MINIMUM_PER_TYPE = 3


def _counts(task_type):
    return {"task_type": task_type, "planned": 0, "missing": 0,
            **{key: {"yes": 0, "no": 0, "unknown": 0} for key in METRICS}}


def _native_metrics(task, observation):
    """Retain measured fields when only an ancillary measurement is unavailable."""
    if observation.get("error"):
        return {}, ["execution_error"]

    pages = observation.get("tool_pages")
    final_ids = observation.get("final_ids")
    if not isinstance(pages, list) or any(type(page) is not int and page is not None for page in pages):
        return {}, ["invalid_tool_trace"]
    if "final_ids" not in observation or (final_ids is not None and not (
            isinstance(final_ids, list) and all(isinstance(item, str) for item in final_ids))):
        return {}, ["invalid_answer_record"]

    row = score(task, observation)
    end = observation.get("termination_reason")
    diagnostics = row["answer_diagnostics"]
    strict_json = diagnostics["format"] == "strict_json" if diagnostics is not None else None
    within_scope = not (row["out_of_range_reads"] or row["beyond_scope_reads"])
    metrics = {"tool_use": row["retrieval_complete"] and within_scope,
               "strict_json": strict_json, "strict_success": row["success"]}
    issues = []
    if end == "model_call_budget":
        metrics.update(natural_stop=False, strict_json=None, strict_success=False)
        if final_ids is not None or observation.get("answer_diagnostics") is not None:
            issues.append("conflicting_budget_record")
    elif end == "output_limit":
        # Even a parsable prefix is not a naturally completed task response.
        metrics.update(natural_stop=False, strict_success=False)
    elif end == "completed":
        metrics["natural_stop"] = True
        if diagnostics is None:
            issues.append("missing_answer_diagnostics")
    else:
        issues.append("unknown_termination")

    if observation.get("answer_diagnostics") is not None and diagnostics is None and end != "model_call_budget":
        issues.append("invalid_answer_diagnostics")
    if strict_json is not None and strict_json != (final_ids is not None):
        issues.append("inconsistent_answer_record")
        metrics.update(strict_json=None, strict_success=None)
    return metrics, issues


def calibration_result(tasks, observations, *, fixture):
    """Interpret planned native trials, retaining unknowns and coverage.

    A trial error or incomplete evidence takes precedence over the sample rule.
    A completed non-JSON model answer is an observed failure. Budget exhaustion
    and output truncation are known failures. Missing final-text evidence is
    expected at the budget boundary. Each task type must have three planned
    trials before labelling a batch floor, ceiling, or mixed. These labels only
    describe the observed strict outcomes; a fixture is always identified.
    """
    groups = {kind: _counts(kind) for kind in TASK_TYPES}
    overall = _counts("overall")
    manifest = {}
    for task in tasks:
        task_id, kind = task["task_id"], task["task_type"]
        if not isinstance(task_id, str) or not task_id or task_id in manifest or kind not in groups:
            raise ValueError("calibration requires unique task IDs and supported pagination task types")
        manifest[task_id] = task

    issues = Counter()
    seen = {}
    duplicates = set()
    for obs in observations:
        if not isinstance(obs, dict) or not isinstance(obs.get("task_id"), str):
            issues["invalid_observation"] += 1
        elif obs["task_id"] not in manifest or obs.get("arm") != "native":
            issues["unexpected_observation"] += 1
        elif obs["task_id"] in seen:
            duplicates.add(obs["task_id"])
            issues["duplicate_observation"] += 1
        else:
            seen[obs["task_id"]] = obs

    for task_id, task in manifest.items():
        if task_id in duplicates:
            # Selecting one of several observations changes the sampling unit.
            metrics, missing = {}, True
        elif task_id not in seen:
            metrics, missing = {}, True
            issues["missing_observation"] += 1
        else:
            metrics, problems = _native_metrics(task, seen[task_id])
            missing = bool(problems)
            issues.update(problems)
        for group in (groups[task["task_type"]], overall):
            group["planned"] += 1
            group["missing"] += int(missing)
            for field in METRICS:
                value = metrics.get(field)
                outcome = "unknown" if value is None else ("yes" if value else "no")
                group[field][outcome] += 1

    if issues:
        status = "incomplete"
    elif any(g["planned"] < MINIMUM_PER_TYPE for g in groups.values()):
        status = "insufficient_sample"
    elif overall["strict_success"]["yes"] == 0:
        status = "floor"
    elif overall["strict_success"]["no"] == 0:
        status = "ceiling"
    else:
        status = "mixed"
    return {"status": status, "fixture": fixture, "provisional": True,
            "minimum_per_type": MINIMUM_PER_TYPE, "groups": list(groups.values()),
            "overall": overall, "issues": dict(sorted(issues.items()))}


def calibration_report(result):
    """Render aggregate evidence only; keep identifiers and raw responses private."""
    status_text = {
        "incomplete": "存在缺失、归属异常或无效观测，校准证据不完整。",
        "insufficient_sample": "至少一类任务未达到最低计划样本数。",
        "floor": "该批次全部可评分任务均未严格成功。",
        "ceiling": "该批次全部任务均严格成功。",
        "mixed": "该批次同时观察到严格成功和失败。",
    }
    source = "本地脚本化模型（fixture）" if result["fixture"] else "实际模型"
    status = result["status"]
    lines = ["# Native 分页校准", "",
             f"**{status}**。{status_text[status]} 使用{source}。"
             "该判断仅描述本批次的原生表现。", "",
             "| 任务类型 | 计划 | 必需页读全且读取范围正确 | 自然停止 | 严格 JSON | 严格任务成功 | 证据缺失或异常的任务 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for group in [*result["groups"], result["overall"]]:
        cells = []
        for metric in METRICS:
            counts = group[metric]
            cells.append(f"{counts['yes']} / {counts['no']} / {counts['unknown']}")
        lines.append(f"| {group['task_type']} | {group['planned']} | " +
                     " | ".join(cells) + f" | {group['missing']} |")
    lines.extend(["", "四个指标各列依次列出是、否、未知的任务数。"
                  "工具项允许重复读取范围内的页。自然停止要求最终回答正常结束。"
                  "调用预算耗尽和输出长度截断均计入严格失败。"
                  f"每类至少计划 {result['minimum_per_type']} 个任务，并取得完整记录后，才按严格成功数描述样本分布。"])
    if result["issues"]:
        details = "，".join(f"`{key}` × {n}" for key, n in result["issues"].items())
        lines.extend(["", f"记录问题包括 {details}。"])
    return "\n".join(lines) + "\n"
