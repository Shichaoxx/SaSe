// Native Pi adapter. The core receives only scope IDs, declared goals, page
// flags and evidence IDs. No hook mutates the session manager or tool result.
import { createHash, randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { VERSION, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { CoreClient } from "./client.mjs";

type Goal = "all" | "first" | "unknown";
type Scope = { workspace: string; session: string; run: string; branch: string };
type Decision = { decision_id: string; assignment: "inject" | "skip"; patch: string | null;
  state: { event_ids: string[] } };

const hash = (s: string) => createHash("sha256").update(s).digest("hex");
// Verified using the pinned Pi runtime and the loopback provider.
const visibleApi = "openai-completions";

function goalFromText(text: string, fallback?: string): Goal {
  const firstLine = text.split(/\r?\n/, 1)[0].trim();
  const goal = firstLine.startsWith("sase.goal=") ? firstLine.slice(10) : fallback;
  return goal === "all" || goal === "first" ? goal : "unknown";
}

function newCall(seq: number, tools: string[] = [], evidenceIds: string[] = ["goal"]) {
  return { id: randomUUID(), seq, tools, evidenceIds, completed: new Set<string>(),
    decision: undefined as Decision | undefined, buildId: undefined as string | undefined };
}

function newTask(scope: Scope) {
  return { scope, call: newCall(0) };
}

function observed(payload: unknown, patch: string): boolean {
  if (!payload || typeof payload !== "object") return false;
  const data = payload as Record<string, unknown>;
  // Inspect user text in supported request envelopes only. Avoid matching a
  // tool argument, model output, schema, header or an unrelated payload field.
  const messages = data.messages;
  if (!Array.isArray(messages)) return false;
  return messages.some(message => {
    if (message?.role !== "user") return false;
    if (typeof message.content === "string") return message.content.includes(patch);
    return Array.isArray(message.content) && message.content.some((block: any) =>
      block?.type === "text" &&
      typeof block.text === "string" && block.text.includes(patch));
  });
}

export default function sase(pi: ExtensionAPI) {
  let client: CoreClient | undefined;
  let task: ReturnType<typeof newTask> | undefined;
  let envGoal: string | undefined;
  let workspace = "";
  let branch = "";
  const problems = new Set<string>();

  function diagnostic(message: string) {
    if (!problems.has(message)) {
      problems.add(message);
      process.stderr.write(`[SaSe] ${message}\n`);
    }
  }

  pi.on("session_start", (event, ctx) => {
    if (VERSION !== "1.6.0") {
      diagnostic("Unsupported Pi version; extension is inactive.");
      return;
    }
    try {
      workspace = process.env.SASE_WORKSPACE || hash(realpathSync(ctx.cwd)).slice(0, 24);
      branch = ctx.sessionManager.getLeafId() || "root";
      envGoal = event.reason === "startup" ? process.env.SASE_GOAL : undefined;
      client = new CoreClient({
        db: process.env.SASE_DB || join(homedir(), ".sase", "sase.sqlite3"),
        timeoutMs: Number(process.env.SASE_TIMEOUT_MS || 200),
      });
    } catch {
      diagnostic("Sidecar could not start; continuing in Pi.");
    }
  });

  pi.on("session_shutdown", async () => {
    task = undefined;
    const old = client;
    client = undefined;
    await old?.close();
  });

  pi.on("session_tree", event => {
    branch = event.newLeafId || "root";
    task = undefined;
    envGoal = undefined;
  });
  // Compaction rebuilds native context. Keep the logical call and its frozen
  // assignment for a retry, then observe exposure afresh in the rebuilt request.
  pi.on("before_agent_start", () => { task = undefined; });
  pi.on("agent_settled", () => { task = undefined; });

  function begin(ctx: ExtensionContext, goal: Goal) {
    const scope = { workspace, session: ctx.sessionManager.getSessionId(), run: randomUUID(), branch };
    const next = newTask(scope);
    task = next;
    void client?.request({ ...scope, op: "event", event_id: "goal", seq: 0, kind: "goal", value: goal })
      .catch(() => {
        diagnostic("Goal acknowledgement unavailable.");
      });
    return next;
  }

  pi.on("message_start", (event, ctx) => {
    if (!client || event.message.role !== "user") return;
    const content = event.message.content;
    const text = typeof content === "string" ? content :
      content.find(block => block.type === "text")?.text || "";
    begin(ctx, goalFromText(text, envGoal));
    // An interactive follow-up or steering message is a fresh task.
    envGoal = undefined;
  });

  pi.on("tool_execution_end", event => {
    if (!client || !task || event.parentToolCallId) return;
    const owner = task;
    const call = owner.call;
    const toolId = hash(event.toolCallId);
    call.completed.add(toolId);
    const next = event.result?.details?.has_next;
    const value = event.toolName === "sase_page" && !event.isError && typeof next === "boolean"
      ? next : "unknown";
    void client.request({ ...owner.scope, op: "event", event_id: `${call.id}.${toolId}`,
      seq: call.seq + 1, kind: "page", tool_call_id: toolId, value }).catch(() => {
      diagnostic("Tool feedback acknowledgement unavailable.");
    });
  });

  // Pi retries can re-enter the loop with turnIndex reset. Advance only when
  // the prior response completed. Lower-level HTTP retries may reuse a payload.
  pi.on("turn_end", event => {
    if (task && event.outcome === "completed") {
      const previous = task.call;
      const tools = [...previous.completed];
      task.call = newCall(previous.seq + 1, tools, ["goal", ...tools.map(id => `${previous.id}.${id}`)]);
    }
  });

  function receipt(owner: ReturnType<typeof newTask>, decision: Decision, buildId: string,
                   kind: "delivered" | "observed", location: "context" | "request_payload") {
    const id = randomUUID();
    void client?.request({ ...owner.scope, op: "receipt", decision_id: decision.decision_id,
      receipt_id: id, attempt_id: buildId, kind, location }).catch(() => diagnostic("Evidence receipt unavailable."));
  }

  pi.on("context", async (event, ctx) => {
    if (!client || ctx.signal?.aborted) return;
    const owner = task ?? begin(ctx, "unknown");
    const call = owner.call;
    // A context build is observable. It can underlie multiple HTTP attempts.
    const buildId = `context-${randomUUID()}`;
    call.buildId = buildId;
    const api = ctx.model?.api || "";
    try {
      const d: Decision = call.decision ?? await client.request({
        ...owner.scope, op: "decide", call_id: call.id, episode_id: call.id,
        as_of_seq: call.seq, host: { name: "pi", version: VERSION },
        // No previous tool result => select an empty evidence window. Parallel
        // results share their sequence; the core leaves ambiguous joins unknown.
        ...(call.tools.length <= 1 ? { tool_call_id: call.tools[0] ?? `missing-${call.id}` } : {}),
        capabilities: { observeTools: true, observeModelRequests: api === visibleApi, injectContext: true,
          confirmExposure: api === visibleApi, nativeSkillEvolution: false },
      });
      if (task !== owner || task.call !== call || call.buildId !== buildId || ctx.signal?.aborted) return;
      call.decision = d;
      if (d.assignment !== "inject" || !d.patch) return;
      // A delayed event ACK can time out after SQLite has committed it. Use
      // the frozen snapshot itself to prove all required facts were present.
      if (!call.evidenceIds.every(id => d.state.event_ids.includes(id))) return;
      const exists = event.messages.some(m => m.role === "custom" && m.customType === "sase.recall" &&
        m.content === d.patch);
      const messages = exists ? event.messages : [...event.messages, {
        role: "custom" as const, customType: "sase.recall", content: d.patch,
        display: false, timestamp: Date.now(),
      }];
      receipt(owner, d, buildId, "delivered", "context");
      return { messages };
    } catch {
      diagnostic("Decision unavailable or timed out; continuing in Pi.");
    }
  });

  pi.on("before_provider_request", (event, ctx) => {
    const d = task?.call.decision;
    if (task?.call.buildId && d?.patch && ctx.model?.api === visibleApi && observed(event.payload, d.patch)) {
      receipt(task, d, task.call.buildId, "observed", "request_payload");
    }
  });
}
