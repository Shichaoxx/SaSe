"""Frozen synthetic pagination tasks, arm-blind scoring and matched summaries."""

from collections import Counter
from hashlib import sha256
from itertools import combinations


TASK_TYPES = ("all-more", "first-more", "all-last")
ARMS = ("native", "fixed", "gated")


def cases(seed=0, per_type=1):
    """Emit reproducible tasks. Related goal variants share their first page."""
    if type(seed) is not int or type(per_type) is not int or per_type < 1:
        raise ValueError("seed must be an integer and per_type must be positive")
    tasks = []
    for index in range(per_type):
        tag = f"pagination-v1/{seed}/{index}"
        layout = sha256(tag.encode()).digest()
        pages = []
        # Each trial has exactly one post-first-page choice in the matched design.
        for page in range(2):
            records = []
            for row in range(1 + layout[page] % 3):
                digest = sha256(f"{tag}/{page}/{row}".encode()).hexdigest()
                records.append("rec-" + digest[:20])
            pages.append(records)
        for kind in TASK_TYPES:
            goal = "first" if kind == "first-more" else "all"
            instruction = ("Read only the first page and return its record IDs."
                           if goal == "first" else "Read all records and return their IDs.")
            tasks.append({
                "task_id": f"pagination-v1-{seed}-{index:04d}-{kind}",
                "task_type": kind,
                "goal": goal,
                "pages": [page.copy() for page in (pages[:1] if kind == "all-last" else pages)],
                "prompt": (f"sase.goal={goal}\n{instruction} Use the sase_page tool, starting at page 1. "
                           'Return a single JSON object {"ids":[...]} containing each requested '
                           'record ID exactly once in any order.'),
            })
    return tasks


def score(case, observation):
    """Grade only the requested records and the executed page requests.

    Repeated in-scope reads cost extra calls. Reading beyond the requested
    scope, omitting a required read, or returning an inexact result fails.
    An execution/trace error leaves success unknown.
    """
    needed = case["pages"][:1] if case["goal"] == "first" else case["pages"]
    expected = {record for page in needed for record in page}
    expected_pages = set(range(1, len(needed) + 1))
    pages = observation.get("tool_pages")
    final_ids = observation.get("final_ids")
    pages_known = isinstance(pages, list)
    valid_answer = isinstance(final_ids, list) and all(isinstance(x, str) for x in final_ids)
    reasons = []
    missing_ids = duplicate_ids = unexpected_ids = None
    if valid_answer:
        counts = Counter(final_ids)
        missing_ids = sorted(expected - counts.keys())
        duplicate_ids = sorted(k for k, v in counts.items() if v > 1)
        unexpected_ids = sorted(counts.keys() - expected)
        if missing_ids:
            reasons.append("missing_ids")
        if duplicate_ids:
            reasons.append("duplicate_ids")
        if unexpected_ids:
            reasons.append("unexpected_ids")
    else:
        reasons.append("invalid_final_answer")

    missing_pages = out_of_range_reads = beyond_scope_reads = extra_calls = None
    if pages_known:
        read = set()
        out_of_range_reads = beyond_scope_reads = 0
        for page in pages:
            if type(page) is not int or page < 1 or page > len(case["pages"]):
                out_of_range_reads += 1
            elif page not in expected_pages:
                beyond_scope_reads += 1
            else:
                read.add(page)
        missing_pages = sorted(expected_pages - read)
        extra_calls = len(pages) - len(read)
        if out_of_range_reads:
            reasons.append("out_of_range_read")
        if beyond_scope_reads:
            reasons.append("beyond_scope_read")
        if missing_pages:
            reasons.append("missing_pages")
    else:
        reasons.append("missing_tool_trace")

    error = observation.get("error")
    status = "missing" if error or not pages_known else "scored"
    if error:
        reasons = ["execution_error"]
    return {
        "status": status,
        "success": not reasons if status == "scored" else None,
        "exact_ids": None if error and final_ids is None else (
            valid_answer and not (missing_ids or duplicate_ids or unexpected_ids)),
        "missing_ids": missing_ids,
        "duplicate_ids": duplicate_ids,
        "unexpected_ids": unexpected_ids,
        "tool_calls": len(pages) if pages_known else None,
        "extra_calls": extra_calls,
        "out_of_range_reads": out_of_range_reads,
        "beyond_scope_reads": beyond_scope_reads,
        "missing_pages": missing_pages,
        "reasons": reasons,
    }


def _pair(left, right, pairs):
    comparable = [(a, b) for a, b in pairs if a["success"] is not None and b["success"] is not None]
    wins = sum(not a["success"] and b["success"] for a, b in comparable)
    losses = sum(a["success"] and not b["success"] for a, b in comparable)
    token_deltas = [b["total_tokens"] - a["total_tokens"] for a, b in comparable
                    if a["total_tokens"] is not None and b["total_tokens"] is not None]
    return {
        "baseline": left, "arm": right, "planned_pairs": len(pairs), "scored_pairs": len(comparable),
        "wins": wins, "losses": losses, "ties": len(comparable) - wins - losses,
        "success_delta_pp": 100 * (wins - losses) / len(comparable) if comparable else None,
        "token_pairs": len(token_deltas),
        "mean_token_delta": sum(token_deltas) / len(token_deltas) if token_deltas else None,
    }


def summarize(tasks, observations, arms=ARMS):
    """Use the planned grid as denominator, including missing/unstarted trials."""
    tasks = list(tasks)
    arms = list(arms)
    by_id = {task["task_id"]: task for task in tasks}
    if not tasks or len(tasks) != len(by_id) or not arms or len(set(arms)) != len(arms) or "native" not in arms:
        raise ValueError("expected unique tasks and arms including native")
    seen = {}
    for offset, obs in enumerate(observations):
        key = (obs["task_id"], obs["arm"])
        if key[0] not in by_id or key[1] not in arms or key in seen:
            raise ValueError("unexpected or duplicate trial")
        seen[key] = (obs, f"observations.json#/{offset}")

    trials = {}
    failures = []
    for task in tasks:
        for arm in arms:
            key = (task["task_id"], arm)
            obs, observation_ref = seen.get(key, ({"error": "not_run"}, None))
            row = score(task, obs)
            tokens = obs.get("total_tokens")
            row["total_tokens"] = tokens if type(tokens) is int and tokens >= 0 else None
            trials[key] = row
            if row["success"] is not True:
                evidence = [{k: d.get(k) for k in ("decision_id", "assignment", "reason", "exposure_status",
                                                  "receipt_locations")}
                            for d in obs.get("decisions", [])]
                failures.append({
                    "task_id": key[0], "arm": arm, "trace_ref": obs.get("trace_ref"),
                    "observation_ref": observation_ref,
                    "task_type": task["task_type"], "error": obs.get("error"),
                    "decisions": evidence, "tool_pages": obs.get("tool_pages"), **row,
                })

    groups = []
    comparisons = []
    kinds = list(dict.fromkeys(task["task_type"] for task in tasks))
    for kind in [*kinds, "overall"]:
        ids = [task["task_id"] for task in tasks if kind == "overall" or task["task_type"] == kind]
        for arm in arms:
            rows = [trials[(i, arm)] for i in ids]
            graded = [r for r in rows if r["success"] is not None]
            wins = sum(r["success"] for r in graded)
            missing = len(rows) - len(graded)
            group = {
                "task_type": kind, "arm": arm, "planned": len(rows), "scored": len(graded),
                "successes": wins, "failures": len(graded) - wins, "missing": missing,
                "success_rate_scored": wins / len(graded) if graded else None,
                "success_rate_bounds": [wins / len(rows), (wins + missing) / len(rows)],
            }
            for metric in ("tool_calls", "extra_calls", "out_of_range_reads", "beyond_scope_reads"):
                group["mean_" + metric] = sum(r[metric] for r in graded) / len(graded) if graded else None
            groups.append(group)
        for left, right in combinations(arms, 2):
            if right == "native":
                left, right = right, left
            pairs = [(trials[(i, left)], trials[(i, right)]) for i in ids]
            comparisons.append({"task_type": kind, **_pair(left, right, pairs)})
    return {"groups": groups, "comparisons": comparisons, "failures": failures}


def _number(value):
    return "—" if value is None else f"{value:.2f}"


def _cell(value):
    return str(value).replace("|", r"\|").replace("\n", " ")


def report(summary):
    """Human-readable report; structured JSON retains each missing ID and reference."""
    lines = ["# 分页任务配对评测", "",
             "任务成功要求完整返回目标范围内的记录 ID，每条恰好一次，并实际读取全部必需页。"
             "读取越界页或额外读取目标范围之外的页均记为失败。重复读取目标页计入额外调用。", "",
             "| 任务类型 | 策略 | 计划 | 成功 | 失败 | 缺失 | 成功率范围 | 平均工具调用 | 平均额外调用 | 平均无效页读取 | 平均超目标读取 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in summary["groups"]:
        lower, upper = row["success_rate_bounds"]
        metrics = " | ".join(_number(row[k]) for k in (
            "mean_tool_calls", "mean_extra_calls", "mean_out_of_range_reads", "mean_beyond_scope_reads"))
        lines.append(f"| {row['task_type']} | {row['arm']} | {row['planned']} | {row['successes']} | "
                     f"{row['failures']} | {row['missing']} | {100*lower:.1f}%–{100*upper:.1f}% | {metrics} |")

    lines.extend(["", "成功率范围按缺失运行全部失败或全部成功计算。调用量取已评分运行均值。", "",
                  "## 同任务配对差值", "",
                  "| 任务类型 | 策略减基线 | 完整配对 / 计划 | 胜 / 平 / 负 | 成功率差（百分点） | Token 配对数 | 平均额外 Token |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
    for row in summary["comparisons"]:
        lines.append(f"| {row['task_type']} | {row['arm']} − {row['baseline']} | "
                     f"{row['scored_pairs']} / {row['planned_pairs']} | "
                     f"{row['wins']} / {row['ties']} / {row['losses']} | {_number(row['success_delta_pp'])} | "
                     f"{row['token_pairs']} | {_number(row['mean_token_delta'])} |")
    lines.extend(["", "差值仅使用同一任务上双侧均已评分的配对。"
                  "Token 差值还要求双方都有用量记录。结果描述本批任务、模型与运行配置。", "",
                  "## 失败与缺失", ""])
    if not summary["failures"]:
        lines.append("本批次所有计划运行均通过评分。")
    else:
        lines.extend(["| 任务 | 策略 | 原因 | 实际读取页 | 轨迹引用 | 决策与曝光 |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for row in summary["failures"]:
            evidence = ", ".join(f"{d['decision_id']} ({d['assignment']}, {d['exposure_status']})"
                                 for d in row["decisions"]) or "—"
            reason = row["error"] or ", ".join(row["reasons"])
            columns = [row["task_id"], row["arm"], reason, row["tool_pages"],
                       ", ".join(x for x in (row["observation_ref"], row["trace_ref"]) if x) or "—", evidence]
            lines.append("| " + " | ".join(_cell(col) for col in columns) + " |")
    return "\n".join(lines) + "\n"
