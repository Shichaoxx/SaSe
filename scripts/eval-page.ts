// Run in Pi's evaluation process. All pages and record IDs are synthetic.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

export default function evalPage(pi: ExtensionAPI) {
  const pages: string[][] = JSON.parse(process.env.SASE_EVAL_PAGES!);
  // Pi normally includes the temporary working directory in its structured
  // prompt. Use identical instructions in all arms, including native.
  pi.on("before_agent_start", () => ({systemPrompt: process.env.SASE_EVAL_SYSTEM_PROMPT!}));
  // The final allowed response is charged even if it asks for more tools.
  // Abort at the assistant message boundary, before any requested tool runs.
  // Pi awaits this hook and propagates its abort signal through the tool
  // batch and transport, including batches with multiple parallel calls.
  const limit = Number(process.env.SASE_EVAL_MAX_MODEL_CALLS);
  let replies = 0;
  pi.on("message_end", ({message}, ctx) => {
    if (message.role !== "assistant") return;
    replies++;
    if (replies === limit && message.content.some(part => part.type === "toolCall")) ctx.abort();
  });
  pi.registerTool({
    name: "sase_page",
    label: "Read a page",
    description: "Retrieve records by page. Start at page 1. A next_cursor, when present, is the page number for the next request.",
    parameters: Type.Object({page: Type.Integer({minimum: 1, description: "Page number to read."})}),
    async execute(_id, {page}) {
      const ids = pages[page - 1];
      if (!ids) throw new Error("Page does not exist.");
      const has_next = page < pages.length;
      const result = {page, ids, next_cursor: has_next ? page + 1 : null};
      return {content: [{type: "text", text: JSON.stringify(result)}], details: {page, has_next}};
    },
  });
}
