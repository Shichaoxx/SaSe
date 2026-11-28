// Only the final assistant text is inspected. Raw text and tool/thinking blocks
// stay in the runner. Unknown strings retain equality through their digest.
import {createHash} from "node:crypto";

function safeId(id, known) {
  return known.has(id) ? id : "unknown:" + createHash("sha256").update(id).digest("hex");
}

function isFencedJson(text) {
  const fenced = /^```(?:json)?[^\S\n]*\n([\s\S]*?)\n```$/i.exec(text);
  if (!fenced) return false;
  try {
    JSON.parse(fenced[1]);
    return true;
  } catch { return false; }
}

export function analyzeAnswer(message, task) {
  const unknown = {final_ids: null, answer_diagnostics: null};
  if (message?.role !== "assistant" || !Array.isArray(message.content) ||
      ["toolUse", "error", "aborted"].includes(message.stopReason) ||
      message.content.some(block => block.type === "toolCall")) return unknown;

  const text = message.content.filter(block => block.type === "text").map(block => block.text).join("").trim();
  const known = new Set(task.pages.flat());
  // This exact schema controls the primary score. Extracting mentions below is
  // independent: a schema-invalid response never receives a parsed final_ids.
  let format, finalIds = null;
  try {
    const answer = JSON.parse(text);
    if (answer && Object.keys(answer).length === 1 && Array.isArray(answer.ids) &&
        answer.ids.every(id => typeof id === "string")) {
      format = "strict_json";
      finalIds = answer.ids.map(id => safeId(id, known));
    } else {
      format = "json_wrong_schema";
    }
  } catch {
    format = text ? (isFencedJson(text) ? "fenced_json" : "text") : "empty";
  }
  const counts = {};
  // Never match the prefix of a malformed or embedded identifier. Preserve
  // spelling and multiplicity even when the model invents a synthetic ID.
  for (const [id] of text.matchAll(/(?<![A-Za-z0-9_-])rec-[0-9a-fA-F]+(?![A-Za-z0-9_-])/g)) {
    const key = safeId(id, known);
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return {final_ids: finalIds, answer_diagnostics: {format, mention_counts: counts}};
}
