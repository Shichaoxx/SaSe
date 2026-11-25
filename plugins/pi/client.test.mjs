import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { CoreClient } from "./client.mjs";

test("a late IPC reply cannot satisfy a later decision", async () => {
  const directory = await mkdtemp(join(tmpdir(), "sase-ipc-"));
  const script = join(directory, "fake-core");
  await writeFile(script, `#!/usr/bin/env python3
import sys,json,os
pending = None
def reply(request):
    print(json.dumps({"id":request["id"],"ok":True,
        "result":{"call":request["call"],"pid":os.getpid()}}),flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request["call"] == "hold":
        pending = request
    else:
        if pending:
            reply(pending)
            pending = None
        reply(request)
`, { mode: 0o700 });
  const client = new CoreClient({ db: join(directory, "db"), python: script, timeoutMs: 2500 });
  try {
    const warm = await client.request({ call: "warm" });
    client.timeoutMs = 120;
    await assert.rejects(client.request({ call: "hold" }), /deadline/);
    const next = await client.request({ call: "next" });
    assert.deepEqual(next, { call: "next", pid: warm.pid });
  } finally {
    await client.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("an unavailable interpreter cannot hang an IPC request or shutdown", async () => {
  const client = new CoreClient({ db: ":memory:", python: "/unavailable/sase-python" });
  await assert.rejects(client.request({ op: "inspect" }), /unavailable/);
  await client.close();
  await assert.rejects(client.request({ op: "inspect" }), /unavailable/);
});
