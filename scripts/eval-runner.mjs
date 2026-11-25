// One fresh Pi task per invocation. The parent owns ordering, scoring and reports.
import {spawn, execFileSync} from "node:child_process";
import {createHash} from "node:crypto";
import {mkdir, mkdtemp, rm, writeFile} from "node:fs/promises";
import http from "node:http";
import {tmpdir} from "node:os";
import path from "node:path";
import {createInterface} from "node:readline";
import {fileURLToPath} from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const pi = path.join(root, "node_modules/.bin/pi");
const python = process.env.SASE_PYTHON || "python3";
const systemPrompt = "Use the supplied tool to answer the user's data request. Follow the requested scope and output format.";

function core(args) {
  return JSON.parse(execFileSync(python, ["-m", "sase", ...args], {
    cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
  }));
}

function fixtureMessages(messages) {
  const tools = messages.filter(m => m.role === "tool");
  return tools.map(m => JSON.parse(typeof m.content === "string" ? m.content : m.content.map(b => b.text).join("")));
}

async function startFixture(goal) {
  const server = http.createServer(async (req, res) => {
    try {
      let body = "";
      for await (const chunk of req) body += chunk;
      const {messages} = JSON.parse(body);
      if (messages[0]?.role !== "system" || messages[0].content !== systemPrompt) {
        throw new Error("fixture system prompt mismatch");
      }
      const records = fixtureMessages(messages);
      const next = records.length ? (goal === "all" ? records.at(-1).next_cursor : null) : 1;
      const response = next ? {
        role: "assistant", tool_calls: [{index: 0, id: "tool-" + next, type: "function",
          function: {name: "sase_page", arguments: JSON.stringify({page: next})}}],
      } : {role: "assistant", content: JSON.stringify({ids: records.flatMap(r => r.ids)})};
      res.writeHead(200, {"content-type": "text/event-stream"});
      for (const [delta, reason] of [[response, null], [{}, next ? "tool_calls" : "stop"]]) {
        const chunk = {id: "fixture", object: "chat.completion.chunk", created: 1, model: "fixture",
          choices: [{index: 0, delta, finish_reason: reason}]};
        res.write("data: " + JSON.stringify(chunk) + "\n\n");
      }
      res.end("data: [DONE]\n\n");
    } catch {
      res.writeHead(400).end();
    }
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  return server;
}

function inputValid(config) {
  const task = config?.case;
  return ["native", "fixed", "gated"].includes(config?.arm) &&
    typeof config.workspace === "string" && config.workspace.length > 0 &&
    typeof config.db === "string" && typeof task?.task_id === "string" &&
    Array.isArray(task.pages) && task.pages.length > 0 &&
    task.pages.every(p => Array.isArray(p) && p.every(id => typeof id === "string")) &&
    typeof task.prompt === "string" && ["all", "first"].includes(task.goal) &&
    Number.isSafeInteger(config.timeoutMs) && config.timeoutMs > 0;
}

function parseIds(message, task) {
  const text = message.content.filter(b => b.type === "text").map(b => b.text).join("").trim();
  let answer;
  try { answer = JSON.parse(text); } catch { return null; }
  if (!answer || Object.keys(answer).length !== 1 || !Array.isArray(answer.ids) ||
      !answer.ids.every(id => typeof id === "string")) return null;
  const known = new Set(task.pages.flat());
  // Preserve excess and duplicates for scoring without publishing arbitrary model text.
  return answer.ids.map(id => known.has(id) ? id : "unknown:" + createHash("sha256").update(id).digest("hex"));
}

function providerError(message) {
  const status = String(message.errorMessage || "").match(/\b(400|401|402|403|404|408|413|422|429|500|502|503|504)\b/);
  return status ? "provider_http_" + status[1] : "provider_error";
}

async function collect(args, env, cwd, config, observation) {
  const child = spawn(pi, args, {env, cwd, stdio: ["ignore", "pipe", "ignore"]});
  let timeout = false;
  const timer = setTimeout(() => { timeout = true; child.kill("SIGKILL"); }, config.timeoutMs);
  const exit = new Promise(resolve => {
    child.once("error", () => resolve(1));
    child.once("close", resolve);
  });
  const usage = {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0};
  const requestedPages = new Map();
  let completeUsage = true, final, error;
  try {
    // Process the native event stream in memory. The harness only retains
    // structured measurements and IDs, including failed/invalid tool attempts.
    for await (const line of createInterface({input: child.stdout})) {
      let event;
      try { event = JSON.parse(line); } catch { continue; }
      if (event.type === "tool_execution_end") {
        const requested = requestedPages.get(event.toolCallId);
        const page = event.result?.details?.page;
        // Preserve out-of-range attempts and distinguish them from completed reads.
        const attemptedOutside = Number.isSafeInteger(requested) &&
          (requested < 1 || requested > config.case.pages.length);
        observation.tool_pages.push(!event.isError && event.toolName === "sase_page" && Number.isSafeInteger(page)
          ? page : attemptedOutside ? requested : null);
        requestedPages.delete(event.toolCallId);
      }
      const msg = event.message;
      if (event.type !== "message_end" || msg?.role !== "assistant") continue;
      observation.model_calls++;
      if (msg.stopReason === "error" || msg.stopReason === "aborted") error = providerError(msg);
      for (const part of msg.content || []) {
        if (part.type !== "toolCall") continue;
        const page = part.arguments?.page;
        if (part.name === "sase_page") requestedPages.set(part.id, page);
      }
      if (!msg.usage || !(msg.usage.totalTokens > 0)) completeUsage = false;
      else for (const field of Object.keys(usage)) usage[field] += msg.usage[field] ?? 0;
      final = msg;
    }
    const code = await exit;
    observation.error = timeout ? "timeout" : error || (code !== 0 || !final ? "runtime_error" : null);
    if (!observation.error) observation.final_ids = parseIds(final, config.case);
    if (!observation.error && completeUsage && final) {
      observation.total_tokens = usage.totalTokens;
      observation.usage = usage;
    }
  } finally {
    clearTimeout(timer);
  }
}

export async function runTrial(config) {
  if (!inputValid(config)) throw new Error("invalid trial configuration");
  const task = config.case;
  const result = {task_id: task.task_id, arm: config.arm, final_ids: null, tool_pages: [],
    total_tokens: null, usage: null, model_calls: 0, trace_ref: null, decisions: [], error: null};
  const cwd = await mkdtemp(path.join(tmpdir(), "sase-trial-"));
  let server;
  try {
    const agent = path.join(cwd, "agent");
    await mkdir(agent, {mode: 0o700});
    const model = config.fixture ? "fixture" : config.model;
    if (typeof model !== "string" || !model) throw new Error();
    if (config.fixture) server = await startFixture(task.goal);
    const url = new URL(server ? "http://127.0.0.1:" + server.address().port + "/v1" : config.baseUrl);
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    if (url.username || url.password || url.search || url.hash ||
        !(url.protocol === "https:" || (loopback && url.protocol === "http:"))) throw new Error();
    const secret = !config.fixture && config.keyEnv ? process.env[config.keyEnv] : undefined;
    if (!loopback && !secret) throw new Error();
    const db = path.resolve(config.db);
    const scope = ["--db", db, "--workspace", config.workspace];
    if (config.arm !== "native") {
      const {skill_version} = core(["import", path.join(root, "examples/paginate/SKILL.md"), ...scope]);
      core(["approve", skill_version, ...scope]);
      result.trace_ref = config.workspace;
    }
    await writeFile(path.join(agent, "models.json"), JSON.stringify({providers: {eval: {
      baseUrl: url.href.replace(/\/$/, ""), api: "openai-completions", apiKey: "$SASE_TRIAL_CREDENTIAL",
      models: [{id: model, reasoning: false, contextWindow: 8192, maxTokens: 768, samplingParams: {temperature: 0}}],
    }}}));
    await writeFile(path.join(agent, "settings.json"), JSON.stringify({
      retry: {enabled: false, provider: {maxRetries: 0}}, compaction: {enabled: false},
    }));
    const args = ["--mode", "json", "--print", "--no-session", "--offline", "--no-context-files",
      "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-approve", "--model", "eval/" + model,
      "--thinking", "off", "--system-prompt", systemPrompt, "--tools", "sase_page",
      "-e", path.join(root, "scripts/eval-page.ts")];
    if (config.arm !== "native") args.push("-e", path.join(root, "plugins/pi/index.ts"));
    args.push("--", task.prompt);
    const env = {PATH: process.env.PATH, PYTHONPATH: root, PI_CODING_AGENT_DIR: agent, PI_OFFLINE: "1",
      SASE_TRIAL_CREDENTIAL: secret || "local-evaluation", SASE_EVAL_SYSTEM_PROMPT: systemPrompt,
      SASE_EVAL_PAGES: JSON.stringify(task.pages),
      SASE_DB: db, SASE_WORKSPACE: config.workspace, SASE_GOAL: task.goal, SASE_PYTHON: python};
    if (config.arm !== "native") env.SASE_EVAL_POLICY = config.arm;
    await collect(args, env, cwd, config, result);
    if (config.arm !== "native") {
      result.decisions = core(["inspect", ...scope]).decisions.map(d => ({
        decision_id: d.decision_id, assignment: d.assignment, exposure_status: d.exposure_status,
        reason: d.reason, policy_version: d.policy_version, skill_version: d.skill_version,
        receipt_locations: d.receipts.map(r => r.location), goal: d.state.goal, has_next: d.state.has_next,
      }));
    }
  } catch { result.error ??= "setup_error"; }
  finally {
    if (server) {
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    }
    await rm(cwd, {recursive: true, force: true});
  }
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    let input = "";
    for await (const chunk of process.stdin) input += chunk;
    console.log(JSON.stringify(await runTrial(JSON.parse(input))));
  } catch {
    console.error("Invalid evaluation trial input.");
    process.exitCode = 2;
  }
}
