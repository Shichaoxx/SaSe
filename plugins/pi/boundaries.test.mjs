// Drive selected pinned Pi hooks directly so retries, branch changes, and
// simultaneous tool completions can be reproduced without timing a model.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import registerSaSe from './index.ts';

const root = fileURLToPath(new URL('../../', import.meta.url));
const user = { role: 'user', content: 'Read the records', timestamp: 1 };

async function host(t, evaluationPolicy) {
  const directory = await mkdtemp(join(tmpdir(), 'sase-boundaries-'));
  const db = join(directory, 'sase.sqlite3');
  const env = { SASE_DB: db, SASE_WORKSPACE: 'boundary-fixture', SASE_GOAL: 'all',
    SASE_TIMEOUT_MS: '2000', SASE_EVAL_POLICY: evaluationPolicy };
  const saved = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]));
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  const ctx = { cwd: directory, model: { api: 'openai-completions' }, sessionManager: {
    getSessionId: () => 'boundary-session', getLeafId: () => 'original',
  } };
  const handlers = new Map();
  registerSaSe({ on: (name, fn) => handlers.set(name, fn) });
  const emit = (type, data = {}) => handlers.get(type)?.({ ...data, type }, ctx);
  t.after(async () => {
    try {
      await emit('session_shutdown');
    } finally {
      for (const [key, value] of Object.entries(saved)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
      await rm(directory, { recursive: true, force: true });
    }
  });
  function cli(...args) {
    return JSON.parse(execFileSync(process.env.SASE_PYTHON || 'python3', ['-m', 'sase', ...args,
      '--db', db, '--workspace', env.SASE_WORKSPACE], { cwd: root, encoding: 'utf8' }));
  }
  const skill = cli('import', join(root, 'examples/paginate/SKILL.md'));
  cli('approve', skill.skill_version);
  await emit('session_start', { reason: 'startup' });
  return {
    emit,
    prompt: text => emit('message_start', { message: { ...user, content: text } }),
    page: (toolCallId, overrides = {}) => emit('tool_execution_end', { toolCallId, toolName: 'sase_page',
      result: { details: { has_next: true } }, isError: false, ...overrides }),
    advance: () => emit('turn_end', { outcome: 'completed' }),
    async context(messages = [user]) {
      const reply = await emit('context', { messages });
      return reply?.messages ?? messages;
    },
    async ledger() {
      await emit('session_shutdown'); // finish queued receipts before inspecting
      return cli('inspect').decisions;
    },
  };
}

function recalls(messages) {
  return messages.filter(message => message.role === 'custom' && message.customType === 'sase.recall');
}

test('context reentry delivers one frozen patch per copy and expires it on the next call', async t => {
  const pi = await host(t);
  await pi.prompt('sase.goal=all');
  await pi.page('page-1');
  await pi.advance();
  const original = Object.freeze([Object.freeze({ ...user })]);
  const first = await pi.context(original);
  assert.equal(recalls(first).length, 1);
  const patch = recalls(first)[0].content;

  // Reentry with the already modified copy and reentry with a fresh copy.
  await pi.emit('turn_end', { outcome: 'error' });
  for (const messages of [first, original]) {
    assert.deepEqual(recalls(await pi.context(messages)).map(m => m.content), [patch]);
  }
  assert.equal(original.length, 1);
  await pi.emit('before_provider_request', { payload: { messages: [{ role: 'tool', content: patch }] } });
  await pi.emit('before_provider_request', { payload: { messages: [{ role: 'user', content: patch }] } });
  await pi.advance();
  assert.deepEqual(recalls(await pi.context(original)), []);
  const decisions = await pi.ledger();
  assert.deepEqual(decisions.map(d => d.assignment), ['inject', 'skip']);
  assert.notEqual(decisions[0].call_id, decisions[1].call_id);
  assert.equal(decisions[1].state.has_next, 'unknown');
  assert.deepEqual(decisions[0].receipts.filter(r => r.kind === 'observed').map(r => r.location),
    ['request_payload']);
});

test('a startup goal is consumed once and fresh prompts and branches own their state', async t => {
  const pi = await host(t);
  await pi.prompt('Use the explicitly configured goal.');
  await pi.page('original');
  await pi.advance();
  assert.equal(recalls(await pi.context()).length, 1);
  await pi.prompt('Read some data.');
  await pi.page('follow-up');
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  await pi.emit('session_tree', { oldLeafId: 'original', newLeafId: 'fork', fromExtension: false });
  assert.deepEqual(recalls(await pi.context()), []);
  await pi.prompt('sase.goal=first');
  await pi.page('fork-page');
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  const records = await pi.ledger();
  assert.deepEqual(records.map(d => [d.branch, d.state.goal, d.state.has_next]), [
    ['original', 'all', true], ['original', 'unknown', true],
    ['fork', 'unknown', 'unknown'], ['fork', 'first', true],
  ]);
  assert.equal(new Set(records.map(d => d.run)).size, 4);
});

test('pagination observations belong to top-level tools and require an unambiguous result', async t => {
  const pi = await host(t);
  await pi.prompt('sase.goal=all');
  await pi.page('child', { parentToolCallId: 'wrapper' });
  await pi.page('wrapper', { toolName: 'unrelated_tool' });
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  await pi.page('top-level');
  await pi.page('nested-side-effect', { parentToolCallId: 'top-level' });
  await pi.advance();
  assert.equal(recalls(await pi.context()).length, 1);
  // Two model-facing page results in the same turn cannot be assigned to a
  // single stream, even when both report the same has_next value.
  await pi.page('page-a');
  await pi.page('page-b');
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  const records = await pi.ledger();
  assert.deepEqual(records.map(d => d.state.has_next), ['unknown', true, 'unknown']);
});

test('fixed evaluation assigns the first successful page-one result once across retries', async t => {
  const pi = await host(t, 'fixed');
  await pi.prompt('sase.goal=first');
  const result = page => ({ result: { details: { has_next: false, page } } });
  await pi.page('failed', { ...result(1), isError: true });
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  await pi.page('later-page', result(2));
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  await pi.page('read-first', result(1));
  await pi.advance();
  const [injected] = recalls(await pi.context());
  assert.ok(injected);
  await pi.emit('turn_end', { outcome: 'error' });
  assert.deepEqual(recalls(await pi.context()).map(m => m.content), [injected.content]);
  await pi.page('read-first-again', result(1));
  await pi.advance();
  assert.deepEqual(recalls(await pi.context()), []);
  const decisions = await pi.ledger();
  assert.deepEqual(decisions.map(d => d.reason), [
    'not_first_page', 'not_first_page', 'fixed_first_page', 'fixed_already_assigned',
  ]);
  assert.ok(decisions.every(d => d.policy_version === 'eval-fixed-first-page-v1'));
});


test('gated evaluation uses exactly the first eligible page-one opportunity, including skips', async t => {
  const pi = await host(t, 'gated');
  for (const [goal, more, expected] of [
    ['all', true, 'continue_pagination'], ['first', true, 'first_page_only'], ['all', false, 'last_page'],
  ]) {
    await pi.prompt('sase.goal=' + goal);
    for (const [toolId, page, hasNext, reason] of [
      ['read-first', 1, more, expected], ['read-higher', 2, true, 'not_first_page'],
      ['read-first-again', 1, true, 'opportunity_used'],
    ]) {
      await pi.page(toolId, { result: { details: { page, has_next: hasNext } } });
      await pi.advance();
      assert.equal(recalls(await pi.context()).length, Number(reason === 'continue_pagination'));
    }
  }
  const decisions = await pi.ledger();
  assert.ok(decisions.every(d => d.policy_version === 'eval-gated-first-page-v1'));
  assert.deepEqual(decisions.map(d => d.reason), ['continue_pagination', 'first_page_only', 'last_page']
    .flatMap(reason => [reason, 'not_first_page', 'opportunity_used']));
  assert.deepEqual(decisions.map(d => d.eligibility), Array(3).fill([true, false, false]).flat());
});
