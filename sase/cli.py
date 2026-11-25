"""Command-line administration and the persistent local JSON Lines transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from .core import Core


MAX_FRAME = 1024 * 1024


def default_workspace():
    return hashlib.sha256(os.fsencode(Path.cwd().resolve())).hexdigest()[:24]


def serve(core, source, sink):
    """One response per line. A damaged frame cannot consume the next request."""
    while line := source.readline(MAX_FRAME + 1):
        request_id = None
        try:
            if len(line) > MAX_FRAME:
                while line and not line.endswith(b"\n"):
                    line = source.readline(MAX_FRAME + 1)
                raise ValueError("oversized frame")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("expected object")
            candidate = request.pop("id", None)
            if type(candidate) not in (str, int) or len(str(candidate)) > 128:
                raise ValueError("invalid request id")
            request_id = candidate
            result = core.handle(request)
            reply = {"id": request_id, "ok": True, "result": result}
        except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
            reply = {"id": request_id, "ok": False, "error": "invalid_request"}
        except sqlite3.Error:
            reply = {"id": request_id, "ok": False, "error": "storage_error"}
        sink.write(json.dumps(reply, separators=(",", ":"), allow_nan=False) + "\n")
        sink.flush()


def doctor(db_path, workspace):
    # The CLI may run from a checkout, an editable install or a virtualenv.
    roots = (Path.cwd(), Path(__file__).resolve().parents[1])
    pi_binary = next((root / "node_modules/.bin/pi" for root in roots
                      if (root / "node_modules/.bin/pi").is_file()), None)
    pi_binary = pi_binary or shutil.which("pi")
    pi = {}
    if pi_binary:
        for parent in Path(pi_binary).resolve().parents:
            manifest = parent / "package.json"
            if manifest.is_file():
                metadata = json.loads(manifest.read_text())
                if metadata.get("name", "").endswith("/pi-coding-agent"):
                    pi = metadata
                    break
    report = {
        "protocol_version": 1,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "database": str(db_path),
        "workspace": workspace,
        "pi_package": pi.get("name"),
        "pi_version": pi.get("version"),
        "pi_binary": str(pi_binary) if pi_binary else None,
        "last_recorded_host": None,
        "last_recorded_capabilities": None,
    }
    if db_path.is_file():
        core = Core(str(db_path))
        try:
            decisions = core.handle({"v": 1, "op": "inspect", "workspace": workspace})["decisions"]
            for decision in reversed(decisions):
                if "capabilities" in decision:
                    report["last_recorded_host"] = decision.get("host")
                    report["last_recorded_capabilities"] = decision["capabilities"]
                    break
        finally:
            core.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sase")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "approve", "inspect", "doctor", "serve"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", type=Path, default=os.environ.get("SASE_DB", "~/.sase/sase.sqlite3"))
        if name != "serve":
            sub.add_argument("--workspace", default=os.environ.get("SASE_WORKSPACE") or default_workspace())
        if name == "import":
            sub.add_argument("file", type=Path)
        if name == "approve":
            sub.add_argument("skill_version")
        if name == "inspect":
            sub.add_argument("--decision-id")
    args = parser.parse_args(argv)
    db_path = args.db.expanduser()
    try:
        if args.command == "doctor":
            result = doctor(db_path, args.workspace)
        else:
            db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            core = Core(str(db_path))
            try:
                if args.command == "serve":
                    serve(core, sys.stdin.buffer, sys.stdout)
                    return 0
                request = {"v": 1, "op": args.command, "workspace": args.workspace}
                if args.command == "import":
                    request["content"] = args.file.read_text(encoding="utf-8")
                elif args.command == "approve":
                    request["skill_version"] = args.skill_version
                elif args.decision_id:
                    request["decision_id"] = args.decision_id
                result = core.handle(request)
            finally:
                core.close()
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"sase: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
