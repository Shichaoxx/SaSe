// Exercise the Pi extension's production IPC client against a temporary ledger.
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { CoreClient } from "../plugins/pi/client.mjs";

const directory = await mkdtemp(path.join(tmpdir(), "sase-bench-"));
const workspace = "benchmark";
const db = path.join(directory, "sase.sqlite3");
const capabilities = {
  observeTools: true, observeModelRequests: true, injectContext: true,
  confirmExposure: true, nativeSkillEvolution: false,
};
const coldStart = performance.now();
const client = new CoreClient({ db, timeoutMs: 2000 });
try {
  await client.request({ op: "inspect", workspace });
  const coldMs = performance.now() - coldStart;
  const content = await readFile(new URL("../examples/paginate/SKILL.md", import.meta.url), "utf8");
  const imported = await client.request({ op: "import", workspace, content });
  await client.request({ op: "approve", workspace, skill_version: imported.skill_version });
  const measurements = {};
  for (const [scenario, goal, next, expected] of [
    ["all_more", "all", true, "inject"], ["all_done", "all", false, "skip"],
    ["first_more", "first", true, "skip"],
  ]) {
    const stream = { workspace, session: "bench", run: scenario, branch: "main" };
    await client.request({ ...stream, op: "event", event_id: "goal", seq: 1, kind: "goal", value: goal });
    await client.request({ ...stream, op: "event", event_id: "page", seq: 2, kind: "page",
      tool_call_id: "read-1", value: next });
    const samples = [];
    for (let index = 0; index < 110; index++) {
      const start = performance.now();
      const decision = await client.request({ ...stream, op: "decide", capabilities,
        as_of_seq: 2, episode_id: scenario, call_id: "call-" + index });
      const ms = performance.now() - start;
      assert.equal(decision.assignment, expected);
      if (index >= 10) samples.push(ms);
    }
    samples.sort((a, b) => a - b);
    measurements[scenario] = { n: samples.length, p95_ms: +samples[94].toFixed(3) };
  }
  console.log(JSON.stringify({ cold_spawn_to_first_reply_ms: +coldMs.toFixed(3), measurements }, null, 2));
  if (Object.values(measurements).some(result => result.p95_ms > 100)) process.exitCode = 1;
} finally {
  await client.close();
  await rm(directory, { force: true, recursive: true });
}
