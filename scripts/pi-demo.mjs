// Real Pi + loopback HTTP. All prompts, tools and model replies are synthetic.
import assert from "node:assert/strict";
import {execFile, execFileSync} from "node:child_process";
import {mkdir, mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import http from "node:http";
import {tmpdir} from "node:os";
import path from "node:path";
import {promisify} from "node:util";
import {fileURLToPath} from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const pi = path.join(root, "node_modules/.bin/pi");
const skill = path.join(root, "examples/paginate/SKILL.md");
const python = process.env.SASE_PYTHON || "python3";
const execute = promisify(execFile);
const cases = [
  {name: "all-more", goal: "all", more: true, reason: "continue_pagination", retry: true},
  {name: "all-done", goal: "all", more: false, reason: "last_page"},
  {name: "first-more", goal: "first", more: true, reason: "first_page_only"},
  {name: "wire-retry", goal: "all", more: true, reason: "continue_pagination", retry: true},
  ...["disabled", "kill", "timeout"].map(fault => ({name: fault, goal: "all", more: true, fault})),
];

function core(args, input) {
  return execFileSync(python, ["-m", "sase", ...args], {cwd: root, encoding: "utf8", input});
}

function inspect(db, workspace) {
  return JSON.parse(core(["inspect", "--db", db, "--workspace", workspace])).decisions;
}

function saveEvidence(db, workspace, events) {
  const lines = events.map((event, id) => JSON.stringify({id, v: 1, workspace, ...event}));
  const replies = core(["serve", "--db", db], lines.join("\n") + "\n").trim().split("\n");
  for (const reply of replies) assert.equal(JSON.parse(reply).ok, true);
}

function answer(res, page) {
  const finish = page ? "tool_calls" : "stop";
  const delta = page ? {role: "assistant", tool_calls: [{index: 0, id: "page-" + page,
    type: "function", function: {name: "sase_page", arguments: JSON.stringify({page})}}]}
    : {role: "assistant", content: "PAGE_TASK_COMPLETE"};
  res.writeHead(200, {"Content-Type": "text/event-stream"});
  for (const [part, reason] of [[delta, null], [{}, finish]]) {
    const chunk = {id: "cmpl-test", object: "chat.completion.chunk", created: 1, model: "fixture",
      choices: [{index: 0, delta: part, finish_reason: reason}]};
    res.write("data: " + JSON.stringify(chunk) + "\n\n");
  }
  res.end("data: [DONE]\n\n");
}

function messageText(messages) {
  return messages.flatMap(m => typeof m.content === "string" ? [m.content]
    : (m.content || []).flatMap(part => part.type === "text" ? [part.text] : [])).join("\n");
}

async function runCase(base, db, scenario) {
  const {goal, more, fault, retry = false, reason, name: workspace} = scenario;
  const cwd = path.join(base, workspace);
  const agent = path.join(cwd, "agent");
  await mkdir(agent, {recursive: true});
  const cli = ["--db", db, "--workspace", workspace];
  const {skill_version} = JSON.parse(core(["import", skill, ...cli]));
  core(["approve", skill_version, ...cli]);
  const trace = path.join(cwd, "native.jsonl");
  const pidFile = path.join(cwd, "sidecar.pid");
  // Mark the PID in the actual Python interpreter for this isolated fault test.
  // Using the same executable preserves the normal cold startup path.
  if (fault === "kill" || fault === "timeout") {
    await writeFile(path.join(cwd, "sitecustomize.py"),
      'import os\nfrom pathlib import Path\nPath(os.environ["SASE_DEMO_PID"]).write_text(str(os.getpid()))\n');
  }

  const requests = [];
  let handlerError, suspendedPid;
  const server = http.createServer(async (req, res) => {
    try {
      assert.equal(req.url, "/v1/chat/completions");
      let body = "";
      for await (const data of req) body += data.toString();
      const {messages} = JSON.parse(body);
      const toolResults = messages.filter(m => m.role === "tool").map(m => JSON.parse(messageText([m])));
      for (const result of toolResults) assert.equal(result.row, "synthetic-row-private");
      const pages = toolResults.map(r => r.page);
      assert.deepEqual(pages, [1, 2].slice(0, pages.length));
      const texts = messageText(messages);
      const markers = [...texts.matchAll(/\[SaSe ([0-9a-f]+) [^\]]+\]/g)];
      const wantPatch = !fault && goal === "all" && more && pages.length === 1;
      assert.equal(markers.length, Number(wantPatch), "unexpected number of skill markers on " + workspace);
      let decisionId = null;
      if (wantPatch) {
        decisionId = markers[0][1];
        const decision = inspect(db, workspace).find(d => d.decision_id === decisionId);
        assert.equal(decision?.assignment, "inject");
        assert.equal(texts.split(decision.patch).length - 1, 1, "full frozen patch must occur once");
      }
      requests.push({pages: pages.length, decisionId});
      if (pages.length === 0 && (fault === "kill" || fault === "timeout")) {
        const pid = Number((await readFile(pidFile, "utf8")).trim());
        assert.ok(Number.isSafeInteger(pid) && pid > 1);
        process.kill(pid, fault === "kill" ? "SIGKILL" : "SIGSTOP");
        if (fault === "timeout") suspendedPid = pid;
      }
      if (suspendedPid && pages.length === 1) {
        process.kill(suspendedPid, "SIGCONT");
        suspendedPid = undefined;
      }
      if (retry && pages.length === 1 && requests.filter(r => r.pages === 1).length === 1) {
        res.writeHead(503, {"Content-Type": "application/json", "retry-after": "0"});
        res.end(JSON.stringify({error: {message: "Synthetic service unavailable"}}));
        return;
      }
      const next = !pages.length ? 1 : pages.length === 1 && more && goal === "all" ? 2 : null;
      answer(res, next);
    } catch (error) {
      handlerError ??= error;
      res.writeHead(400, {"Content-Type": "application/json"});
      res.end(JSON.stringify({error: {message: "Fixture assertion failed"}}));
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  try {
    await writeFile(path.join(agent, "models.json"), JSON.stringify({providers: {local: {
      baseUrl: "http://127.0.0.1:" + server.address().port + "/v1", apiKey: "fixture",
      api: "openai-completions", models: [{id: "fixture"}],
    }}}));
    await writeFile(path.join(agent, "settings.json"), JSON.stringify({
      retry: {enabled: workspace !== "wire-retry", maxRetries: 1, baseDelayMs: 1,
        provider: {maxRetries: workspace === "wire-retry" ? 1 : 0}},
    }));
    const installed = workspace === "all-more";
    const args = ["--print", "--offline", ...(installed ? ["--approve", "--no-mcp"] : ["--no-extensions", "--no-approve"]),
      "--no-skills", "--no-context-files", "--no-prompt-templates", "--session", trace, "--model", "local/fixture",
      "--tools", "sase_page", "-e", path.join(root, "scripts/pi-demo-tool.ts")];
    if (!installed && fault !== "disabled") args.push("-e", path.join(root, "plugins/pi/index.ts"));
    args.push(goal === "all" ? "Retrieve all pages." : "Retrieve page one only.");
    const env = {PATH: process.env.PATH, PI_CODING_AGENT_DIR: agent, PI_OFFLINE: "1",
      PYTHONPATH: [root, cwd].join(path.delimiter), SASE_DB: db, SASE_WORKSPACE: workspace, SASE_GOAL: goal,
      SASE_DEMO_MORE: more ? "1" : "0", SASE_PYTHON: python,
      SASE_DEMO_PID: pidFile, SASE_TIMEOUT_MS: "200"};
    if (installed) {
      execFileSync(pi, ["install", "--local", path.join(root, "plugins/pi")], {env, cwd});
    }
    let output;
    try {
      const run = execute(pi, args, {cwd, env, timeout: 30000, maxBuffer: 1 << 20});
      run.child.stdin.end();
      output = await run;
    } catch (error) {
      if (handlerError && error.stderr) console.error(error.stderr);
      throw handlerError || error;
    }
    if (handlerError) {
      if (output.stderr) console.error(output.stderr);
      throw handlerError;
    }
    assert.match(output.stdout, /PAGE_TASK_COMPLETE/, output.stderr);
    const expected = [0, 1];
    if (retry) expected.push(1);
    if (more && goal === "all") expected.push(2);
    assert.deepEqual(requests.map(r => r.pages), expected, "native call/attempt count");
    assert.ok(!(await readFile(trace, "utf8")).includes("[SaSe "), "persistent native trace contains a patch");

    const decisions = inspect(db, workspace);
    if (fault === "disabled") assert.equal(decisions.length, 0);
    if (fault === "kill" || fault === "timeout") {
      assert.ok(decisions.some(d => d.state.goal === goal), "sidecar was running before fault");
      assert.ok(decisions.every(d => !d.receipts.length), "unapplied patch recorded as delivered");
      assert.ok(output.stderr.includes("[SaSe]"), "expected degradation diagnostic");
    }
    if (!fault) {
      const matched = decisions.filter(d => d.state.has_next === more && d.state.goal === goal);
      assert.equal(matched.length, 1, "single decision for the logical model call, including retry");
      const [decision] = matched;
      assert.equal(decision.reason, reason);
      assert.equal(decision.assignment, reason === "continue_pagination" ? "inject" : "skip");
      assert.equal(decision.skill_version, skill_version);
      assert.ok(decision.state.event_ids.length >= 2);
      const observations = requests.filter(r => r.decisionId);
      if (retry) {
        assert.equal(observations.length, 2);
        assert.ok(observations.every(r => r.decisionId === decision.decision_id));
        assert.ok(decision.receipts.some(r => r.kind === "delivered"));
        assert.ok(decision.receipts.some(r => r.kind === "observed"));
      }
      const evidence = observations.map((_, i) => ({
        op: "receipt", kind: "observed", location: "provider",
        receipt_id: "wire-" + i, attempt_id: "wire-" + i, decision_id: decision.decision_id,
      }));
      // Score the next action against the explicit task and the terminal answer
      // against the recorded tool results. The scripted provider has no treatment effect.
      const expectedPageCount = goal === "all" && more ? 2 : 1;
      const pagesRead = requests.at(-1).pages;
      const outcomes = {
        proximal: requests.some(r => r.pages === 2) === (expectedPageCount === 2),
        distal: pagesRead === expectedPageCount && output.stdout.includes("PAGE_TASK_COMPLETE"),
      };
      for (const [window, value] of Object.entries(outcomes)) {
        evidence.push({op: "outcome", window, outcome_id: "fixture-" + window,
          decision_id: decision.decision_id, value});
      }
      saveEvidence(db, workspace, evidence);
      const saved = inspect(db, workspace).find(d => d.decision_id === decision.decision_id);
      assert.equal(saved.receipts.filter(r => r.location === "provider").length, observations.length);
      assert.equal(saved.distal_outcome.value, true);
      assert.equal(saved.proximal_outcome.value, true);
    }
    console.log(workspace + ": " + (reason || "native task completed") +
      ", HTTP attempts=" + requests.length + ", observed patches=" + requests.filter(r => r.decisionId).length);
  } finally {
    if (suspendedPid) process.kill(suspendedPid, "SIGCONT");
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
}

const base = await mkdtemp(path.join(tmpdir(), "sase-pi-"));
const keep = process.argv.includes("--keep-evidence");
const evidenceDir = keep ? await mkdtemp(path.join(tmpdir(), "sase-evidence-")) : base;
const db = path.join(evidenceDir, "evidence.sqlite3");
try {
  const selected = process.argv.find(arg => arg.startsWith("--case="))?.slice(7);
  const scenarios = selected ? cases.filter(s => s.name === selected) : cases;
  assert.ok(scenarios.length, "unknown demo case");
  for (const scenario of scenarios) await runCase(base, db, scenario);
  assert.ok(!(await readFile(db)).includes("synthetic-row-private"), "raw tool content in ledger");
  if (keep) console.log("Inspect with python3 -m sase inspect --workspace " + scenarios[0].name + " --db " + db);
} finally {
  await rm(base, {force: true, recursive: true});
}
