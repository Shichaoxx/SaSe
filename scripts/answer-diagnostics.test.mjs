import assert from "node:assert/strict";
import test from "node:test";
import {analyzeAnswer} from "./answer-diagnostics.mjs";

const a = "rec-1a2b", b = "rec-cd34";
const task = {pages: [[a], [b]]};

function answer(text) {
  return analyzeAnswer({role: "assistant", content: [{type: "text", text}]}, task);
}

test("strict JSON retains the original parse while diagnostics count final-text mentions", () => {
  assert.deepEqual(answer(JSON.stringify({ids: [a, b, a]})), {
    final_ids: [a, b, a], answer_diagnostics: {format: "strict_json", mention_counts: {[a]: 2, [b]: 1}},
  });
  for (const [format, text] of [
    ["fenced_json", '```json\n{"ids":["' + a + '"]}\n```'],
    ["json_wrong_schema", JSON.stringify({rows: [a]})],
    ["text", "These are the rows: " + a],
  ]) {
    const result = answer(text);
    assert.equal(result.final_ids, null);
    assert.deepEqual(result.answer_diagnostics, {format, mention_counts: {[a]: 1}});
  }
});

test("only standalone synthetic references persist; invented IDs are hashed consistently", () => {
  const secret = "private-raw-text";
  const invented = "rec-deadbeef";
  const result = answer(JSON.stringify({ids: [invented, a, invented, secret, "x" + b, b + "ish", a + "-x"]}));
  const key = result.final_ids[0];
  assert.match(key, /^unknown:[0-9a-f]{64}$/);
  assert.deepEqual(result.answer_diagnostics.mention_counts, {[a]: 1, [key]: 2});
  assert.ok(!JSON.stringify(result).includes(secret));
  assert.ok(!JSON.stringify(result).includes(invented));
});

test("unavailable or tool-only replies produce unknown diagnostics; terminal empty text is observable", () => {
  for (const msg of [null, {role: "tool", content: [{type: "text", text: a}]},
    {role: "assistant", stopReason: "error", content: [{type: "text", text: a}]},
    {role: "assistant", content: [{type: "toolCall", arguments: {text: a}}]}]) {
    assert.deepEqual(analyzeAnswer(msg, task), {final_ids: null, answer_diagnostics: null});
  }
  const onlyVisible = analyzeAnswer({role: "assistant", content: [
    {type: "thinking", thinking: a}, {type: "text", text: b},
  ]}, task);
  assert.deepEqual(onlyVisible.answer_diagnostics.mention_counts, {[b]: 1});
  assert.deepEqual(answer("  ").answer_diagnostics, {format: "empty", mention_counts: {}});
});
