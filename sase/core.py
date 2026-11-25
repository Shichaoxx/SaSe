"""Host-independent projection, skill versions and evidence for protocol v1."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from uuid import uuid4


CAPABILITIES = (
    "observeTools", "observeModelRequests", "injectContext", "confirmExposure",
    "nativeSkillEvolution",
)
SCOPE = ("workspace", "session", "run", "branch")
UNKNOWN = "unknown"
POLICY = "pagination-v1"
FIXED_EVAL_POLICY = "eval-fixed-first-page-v1"
GATED_EVAL_POLICY = "eval-gated-first-page-v1"
MISSING_REASONS = {"unknown", "unavailable", "not_observed", "not_scored",
                   "not_applicable", "cancelled", "timeout", "missing"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identifier(req, field):
    value = req.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or not value.isprintable():
        raise ValueError(f"invalid {field}")
    return value


def sequence(req, field):
    value = req.get(field)
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"invalid {field}")
    return value


def parse_skill(content):
    """The v1 importer accepts single-line name and description frontmatter."""
    if not isinstance(content, str) or len(content.encode()) > 16_384:
        raise ValueError("skill must be UTF-8 text of at most 16 KiB")
    lines = content.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("expected SKILL.md frontmatter")
    end = lines.index("---", 1)
    metadata = {}
    for line in lines[1:end]:
        key, delimiter, value = line.partition(":")
        if not delimiter or key not in ("name", "description"):
            continue
        if key in metadata:
            raise ValueError("duplicate skill metadata")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("unbalanced quoted metadata")
            value = value[1:-1]
        metadata[key] = value
    name = metadata.get("name", "")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("invalid skill name")
    body = "\n".join(lines[end + 1:]).strip()
    if not metadata.get("description") or not body:
        raise ValueError("skill description and body required")
    return name, body


# Schema v1. Events, versions, assignments and receipts are immutable in the
# protocol. Only the current approved version pointer is updated in place.
_SCHEMA = (
    """CREATE TABLE skills (
        workspace TEXT, version TEXT, name TEXT NOT NULL, hash TEXT NOT NULL,
        content TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(workspace,version))""",
    """CREATE TABLE active (
        workspace TEXT, name TEXT, version TEXT NOT NULL, PRIMARY KEY(workspace,name),
        FOREIGN KEY(workspace,version) REFERENCES skills(workspace,version))""",
    """CREATE TABLE events (
        stream TEXT, event_id TEXT, seq INTEGER NOT NULL, kind TEXT NOT NULL,
        value TEXT NOT NULL, tool_call_id TEXT, PRIMARY KEY(stream,event_id))""",
    "CREATE INDEX event_order ON events(stream,kind,seq DESC)",
    """CREATE TABLE decisions (
        decision_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, stream TEXT NOT NULL,
        call_id TEXT NOT NULL, record TEXT NOT NULL, UNIQUE(stream,call_id))""",
    "CREATE INDEX workspace_decisions ON decisions(workspace)",
    """CREATE TABLE evidence (
        decision_id TEXT REFERENCES decisions(decision_id), category TEXT,
        entry_id TEXT, record TEXT NOT NULL, PRIMARY KEY(decision_id,category,entry_id))""",
)


class Core:
    def __init__(self, dbpath):
        self.db = sqlite3.connect(dbpath)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    for statement in _SCHEMA:
                        self.db.execute(statement)
                    self.db.execute("PRAGMA user_version=1")
                elif version != 1:
                    raise ValueError("unsupported database schema")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def handle(self, req):
        if not isinstance(req, dict) or type(req.get("v")) is not int or req["v"] != 1:
            raise ValueError("unsupported protocol")
        workspace = identifier(req, "workspace")
        op = req.get("op")
        handlers = {"import": self._import, "approve": self._approve, "event": self._event,
                    "decide": self._decide, "receipt": self._receipt, "outcome": self._outcome,
                    "inspect": self._inspect}
        if op not in handlers:
            raise ValueError("unknown operation")
        with self.db:
            # A decision and its frozen inputs must be read/committed as one
            # operation, even when several adapters share this database.
            self.db.execute("BEGIN" if op == "inspect" else "BEGIN IMMEDIATE")
            return handlers[op](req, workspace)

    def _import(self, req, workspace):
        content = req.get("content")
        name, body = parse_skill(content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        version = f"{name}@{digest}"
        self.db.execute("INSERT OR IGNORE INTO skills VALUES(?,?,?,?,?,?)",
                        (workspace, version, name, digest, content, body))
        return {"skill_version": version, "content_hash": digest}

    def _approve(self, req, workspace):
        version = identifier(req, "skill_version")
        row = self.db.execute("SELECT name FROM skills WHERE workspace=? AND version=?",
                              (workspace, version)).fetchone()
        if not row:
            raise ValueError("candidate not found in workspace")
        self.db.execute("""INSERT INTO active VALUES(?,?,?) ON CONFLICT(workspace,name)
                           DO UPDATE SET version=excluded.version""", (workspace, row["name"], version))
        return {"skill_version": version, "status": "approved"}

    def _event(self, req, workspace):
        stream = encode([identifier(req, key) for key in SCOPE])
        event_id = identifier(req, "event_id")
        seq = sequence(req, "seq")
        kind, value = req.get("kind"), req.get("value")
        tool = None
        if "value" not in req:
            raise ValueError("expected normalized fact value")
        if value is None:
            value = UNKNOWN
        if kind in ("page", "first_page") and (type(value) is bool or value == UNKNOWN):
            tool = identifier(req, "tool_call_id")
        elif kind != "goal" or value not in ("all", "first", UNKNOWN):
            raise ValueError("unsupported projected fact")
        values = (seq, kind, encode(value), tool)
        existing = self.db.execute(
            "SELECT seq,kind,value,tool_call_id FROM events WHERE stream=? AND event_id=?",
            (stream, event_id)).fetchone()
        if existing and tuple(existing) != values:
            raise ValueError("event id conflict")
        if not existing:
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (stream, event_id, *values))
        return {"event_id": event_id}

    def _latest(self, stream, kind, seq, tool=None):
        # Equal-order results from parallel calls need attribution. Retain all
        # source references at that boundary rather than ordering by arrival.
        params = [stream, kind, seq]
        where = "stream=? AND kind=? AND seq<=?"
        if tool is not None:
            where += " AND tool_call_id=?"
            params.append(tool)
        return self.db.execute(
            f"""SELECT event_id,seq,value,tool_call_id FROM events WHERE {where}
                AND seq=(SELECT MAX(seq) FROM events WHERE {where}) ORDER BY event_id""",
            params + params).fetchall()

    def _project(self, stream, seq, tool, *, current_page_only=False):
        goals = self._latest(stream, "goal", seq)
        pages = self._latest(stream, "page", seq, tool)
        if current_page_only:
            pages = [row for row in pages if row["seq"] == seq]
        if pages and goals and pages[0]["seq"] < goals[0]["seq"]:
            pages = []
        goal_values = {json.loads(row["value"]) for row in goals}
        page_values = {json.loads(row["value"]) for row in pages}
        tool_ids = {row["tool_call_id"] for row in pages}
        unambiguous_page = len(page_values) == 1 and len(tool_ids) == 1
        return {"goal": goal_values.pop() if len(goal_values) == 1 else UNKNOWN,
                "has_next": page_values.pop() if unambiguous_page else UNKNOWN,
                "tool_call_id": tool_ids.pop() if len(tool_ids) == 1 else UNKNOWN,
                "as_of_seq": seq, "projection_version": 1,
                "event_ids": [row["event_id"] for row in goals + pages]}

    @staticmethod
    def _reason(state, caps, skill):
        if not caps["injectContext"] or not caps["observeTools"]:
            return "unsupported_capabilities"
        if UNKNOWN in (state["goal"], state["has_next"]):
            return "unknown_state"
        if state["goal"] == "first":
            return "first_page_only"
        if not state["has_next"]:
            return "last_page"
        return "continue_pagination" if skill else "no_approved_skill"

    def _evaluation_reason(self, state, stream, caps, skill, policy):
        # A successful, uniquely attributed page-one result defines this arm's
        # exposure opportunity. Join position and feedback at the same call;
        # never infer "first page" from the number of observed tool events.
        state["evaluation_opportunity"] = False
        seq = state["as_of_seq"]
        position = self._latest(stream, "first_page", seq, state["tool_call_id"])
        current = [row for row in position if row["seq"] == seq]
        positions = {json.loads(row["value"]) for row in current}
        first = positions.pop() if len(positions) == 1 else UNKNOWN
        state["is_first_page"] = first
        state["event_ids"].extend(row["event_id"] for row in current)
        if not caps["observeTools"] or not caps["injectContext"]:
            return "unsupported_capabilities"
        if first is not True or type(state["has_next"]) is not bool:
            return "not_first_page"
        if not skill:
            return "no_approved_skill"

        # This lookup and allocation share the same write transaction. Once
        # allocated, a missed callback cannot trigger a second assignment.
        prior = self.db.execute("SELECT record FROM decisions WHERE stream=?", (stream,))
        for row in prior:
            record = json.loads(row["record"])
            if record["policy_version"] == policy and record["eligibility"]:
                return "fixed_already_assigned" if policy == FIXED_EVAL_POLICY else "opportunity_used"
        state["evaluation_opportunity"] = True
        if policy == FIXED_EVAL_POLICY:
            return "fixed_first_page"
        return self._reason(state, caps, skill)

    def _decide(self, req, workspace):
        scope = {key: identifier(req, key) for key in SCOPE}
        stream = encode(list(scope.values()))
        call = identifier(req, "call_id")
        row = self.db.execute("SELECT record FROM decisions WHERE stream=? AND call_id=?",
                              (stream, call)).fetchone()
        if row:
            return json.loads(row["record"])
        host = req.get("host")
        if host is not None:
            if not isinstance(host, dict):
                raise ValueError("invalid host")
            host = {key: identifier(host, key) for key in ("name", "version")}
        episode = identifier(req, "episode_id")
        seq = sequence(req, "as_of_seq")
        caps = req.get("capabilities")
        if not isinstance(caps, dict) or any(type(caps.get(k)) is not bool for k in CAPABILITIES):
            raise ValueError("invalid capabilities")
        caps = {k: caps[k] for k in CAPABILITIES}
        tool = identifier(req, "tool_call_id") if "tool_call_id" in req else None
        arm = req.get("evaluation_policy")
        if arm not in (None, "gated", "fixed"):
            raise ValueError("unknown evaluation policy")
        state = self._project(stream, seq, tool, current_page_only=arm is not None)
        skill = self.db.execute("""SELECT s.* FROM active a JOIN skills s
            ON a.workspace=s.workspace AND a.version=s.version
            WHERE a.workspace=? AND a.name='paginate'""", (workspace,)).fetchone()
        if arm is None:
            reason = self._reason(state, caps, skill)
            policy = POLICY
        else:
            policy = FIXED_EVAL_POLICY if arm == "fixed" else GATED_EVAL_POLICY
            reason = self._evaluation_reason(state, stream, caps, skill, policy)
        inject = reason in ("continue_pagination", "fixed_first_page")
        decision_id = uuid4().hex
        version = skill["version"] if skill else None
        record = {
            "v": 1, **scope, "call_id": call, "episode_id": episode, "decision_id": decision_id,
            "state": state, "host": host, "capabilities": caps,
            "eligibility": state.get("evaluation_opportunity", inject),
            "assignment": "inject" if inject else "skip", "reason": reason,
            "randomized": False, "propensity": None, "policy_version": policy,
            "skill_version": version, "content_hash": skill["hash"] if skill else None,
            "patch": f"[SaSe {decision_id} {version}]\n{skill['body']}" if inject else None,
            "exposure_status": UNKNOWN if inject else "not_applicable",
            "proximal_outcome": None, "distal_outcome": None,
        }
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)",
                        (decision_id, workspace, stream, call, encode(record)))
        return record

    def _assigned(self, req, workspace):
        decision = identifier(req, "decision_id")
        row = self.db.execute("SELECT record FROM decisions WHERE workspace=? AND decision_id=?",
                              (workspace, decision)).fetchone()
        if not row:
            raise ValueError("decision not found in workspace")
        record = json.loads(row["record"])
        if any(k in req and req[k] != record[k] for k in SCOPE):
            raise ValueError("decision belongs to another stream")
        return record

    def _append(self, decision, category, entry_id, record):
        params = (decision, category, entry_id)
        serialized = encode(record)
        row = self.db.execute("""SELECT record FROM evidence
            WHERE decision_id=? AND category=? AND entry_id=?""", params).fetchone()
        if row and row[0] != serialized:
            raise ValueError("evidence id conflict")
        if not row:
            self.db.execute("INSERT INTO evidence VALUES(?,?,?,?)", (*params, serialized))
        return {"decision_id": decision, **record}

    def _receipt(self, req, workspace):
        assigned = self._assigned(req, workspace)
        kind = req.get("kind")
        location = req.get("location")
        if (kind, location) not in (("delivered", "context"), ("observed", "request_payload"),
                                    ("observed", "provider")):
            raise ValueError("invalid observation location")
        if assigned["assignment"] != "inject":
            raise ValueError("receipt requires an injection decision")
        caps = assigned["capabilities"]
        if location == "request_payload" and not (caps["observeModelRequests"] and caps["confirmExposure"]):
            raise ValueError("host cannot observe exposure at request boundary")
        entry = {"receipt_id": identifier(req, "receipt_id"), "kind": kind, "location": location}
        if req.get("attempt_id") is not None:
            entry["attempt_id"] = identifier(req, "attempt_id")
        return self._append(assigned["decision_id"], "receipt", entry["receipt_id"], entry)

    def _outcome(self, req, workspace):
        assigned = self._assigned(req, workspace)
        value = req["value"]
        window = req.get("window")
        if window not in ("proximal", "distal") or (value is not None and type(value) is not bool):
            raise ValueError("invalid outcome window or value")
        reason = req.get("missing_reason")
        if (value is None and reason not in MISSING_REASONS) or (value is not None and reason is not None):
            raise ValueError("invalid missing reason")
        entry = {"outcome_id": identifier(req, "outcome_id"), "window": window,
                 "value": value, "missing_reason": reason}
        return self._append(assigned["decision_id"], "outcome", entry["outcome_id"], entry)

    def _inspect(self, req, workspace):
        if req.get("decision_id") is not None:
            decisions = [self._assigned(req, workspace)]
        else:
            decisions = [json.loads(row[0]) for row in self.db.execute(
                "SELECT record FROM decisions WHERE workspace=? ORDER BY rowid", (workspace,))]
        for decision in decisions:
            decision["receipts"] = []
            decision["outcomes"] = []
            for category, record in self.db.execute(
                "SELECT category,record FROM evidence WHERE decision_id=? ORDER BY rowid",
                (decision["decision_id"],)):
                item = json.loads(record)
                if category == "outcome":
                    decision["outcomes"].append(item)
                    decision[f"{item['window']}_outcome"] = item
                else:
                    decision["receipts"].append(item)
                    if item["kind"] == "observed":
                        decision["exposure_status"] = "observed"
        return {"decisions": decisions}
