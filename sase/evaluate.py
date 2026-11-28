"""Run task-paired evaluation against a fresh Pi session in each arm."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from .core import parse_skill
from .evaluation import ARMS, cases, report, summarize


ROOT = Path(__file__).resolve().parents[1]


def schedule(tasks, seed):
    rng = random.Random(seed)
    task_ids = [task["task_id"] for task in tasks]
    rng.shuffle(task_ids)
    trials = []
    for task_id in task_ids:
        arms = list(ARMS)
        rng.shuffle(arms)
        trials.extend({"task_id": task_id, "arm": arm} for arm in arms)
    return trials


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def fingerprints():
    sources = ("examples/paginate/SKILL.md", "plugins/pi/index.ts", "plugins/pi/client.mjs",
               "scripts/eval-page.ts", "scripts/eval-runner.mjs", "scripts/answer-diagnostics.mjs",
               "sase/core.py",
               "sase/evaluation.py", "sase/evaluate.py", "package-lock.json")
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}


def run(args):
    if not (ROOT / "node_modules/.bin/pi").is_file():
        raise ValueError("Run npm ci in the SaSe checkout first.")
    skill = args.skill.read_text(encoding="utf-8")
    name, _ = parse_skill(skill)
    if name != "paginate":
        raise ValueError("This evaluation requires a SKILL.md whose name is paginate.")
    skill_version = "paginate@" + sha256(skill.encode()).hexdigest()
    if args.output is None:
        parent = ROOT / "evaluation-runs"
        parent.mkdir(exist_ok=True)
        out = Path(tempfile.mkdtemp(prefix="batch-", dir=parent))
    else:
        out = args.output.resolve()
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
    snapshot = out / "SKILL.md"
    snapshot.write_bytes(skill.encode())
    tasks = cases(args.seed, args.per_type)
    indexed = {t["task_id"]: t for t in tasks}
    order = schedule(tasks, args.seed)
    mode = "fixture" if args.fixture else "live"
    model = "fixture" if args.fixture else args.model
    manifest = {
        "format": 2, "started_utc": datetime.now(timezone.utc).isoformat(), "mode": mode,
        "model": model, "pi_version": "1.6.0", "seed": args.seed,
        "temperature": 0, "timeout_seconds": args.timeout, "max_model_calls": args.max_model_calls,
        "skill_version": skill_version, "tasks": tasks, "order": order,
        "source_sha256": fingerprints(),
    }
    write_json(out / "manifest.json", manifest)
    observations = []
    print(f"{mode}, {model}, {len(order)} trials → {out}", flush=True)
    try:
        for n, trial in enumerate(order, start=1):
            workspace = trial["task_id"] + "-" + trial["arm"]
            config = {"case": indexed[trial["task_id"]], "arm": trial["arm"],
                      "fixture": args.fixture, "model": model, "baseUrl": args.base_url,
                      "keyEnv": args.key_env, "timeoutMs": args.timeout * 1000,
                      "maxModelCalls": args.max_model_calls, "skillPath": str(snapshot),
                      "expectedSkillVersion": skill_version,
                      "workspace": workspace, "db": str(out / "ledger.sqlite3")}
            try:
                result = subprocess.run(["node", str(ROOT / "scripts/eval-runner.mjs")],
                                        input=json.dumps(config), cwd=ROOT, text=True,
                                        capture_output=True, check=True, timeout=args.timeout + 30)
                observation = json.loads(result.stdout)
                if any(observation.get(k) != v for k, v in trial.items()):
                    raise ValueError("misattributed observation")
            except (OSError, ValueError, subprocess.SubprocessError):
                observation = {**trial, "error": "runner_error"}
            observations.append(observation)
            write_json(out / "observations.json", observations)
            print(f"[{n}/{len(order)}] {workspace}  {observation.get('error') or 'completed'}", flush=True)
    finally:
        summary = summarize(tasks, observations)
        write_json(out / "summary.json", summary)
        description = (f"运行模式 `{mode}`，模型 `{model}`，任务种子 {args.seed}。"
                       f"每次运行最多 {args.max_model_calls} 次模型调用。技能版本为 `{skill_version}`。")
        (out / "report.md").write_text(report(summary).replace("\n", "\n\n" + description + "\n", 1),
                                       encoding="utf-8")
    print(out / "report.md")
    return int(any(obs.get("error") for obs in observations))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fixture", action="store_true", help="local deterministic provider")
    group.add_argument("--model", help="model ID at an OpenAI-compatible chat completions endpoint")
    parser.add_argument("--base-url", help="provider API base, e.g. http://127.0.0.1:8080/v1")
    parser.add_argument("--key-env", help="name of the environment variable holding the provider key")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--per-type", type=int, default=1, help="number of tasks per task type")
    parser.add_argument("--timeout", type=int, default=90, help="per-trial deadline in seconds")
    parser.add_argument("--max-model-calls", type=int, default=6,
                        help="model-call budget shared by all arms (per trial)")
    parser.add_argument("--skill", type=Path, default=ROOT / "examples/paginate/SKILL.md",
                        help="pagination skill to freeze for fixed and gated trials")
    parser.add_argument("--output", type=Path, help="new local directory for evidence")
    args = parser.parse_args(argv)
    if min(args.timeout, args.per_type, args.max_model_calls) < 1:
        parser.error("--timeout, --per-type and --max-model-calls must be positive")
    if args.fixture and (args.base_url or args.key_env):
        parser.error("--fixture uses its own local provider")
    if args.model:
        url = urlsplit(args.base_url or "")
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if not url.hostname or url.username or url.password or url.query or url.fragment or not (
                url.scheme == "https" or local and url.scheme == "http"):
            parser.error("provide a valid --base-url (HTTPS, or HTTP on loopback)")
        if (args.key_env and not os.environ.get(args.key_env)) or (not local and not args.key_env):
            parser.error("configure the provider credential in --key-env")
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    sys.exit(main())
