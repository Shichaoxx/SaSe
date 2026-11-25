// Synthetic tool shared by the native and SaSe runs. Only details.has_next
// belongs in the SaSe state projection. Raw content stays with the host.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

export default function demoPage(pi: ExtensionAPI) {
  pi.registerTool({
    name: "sase_page",
    label: "Read synthetic page",
    description: "Read page 1 or 2 from the local synthetic dataset.",
    parameters: Type.Object({page: Type.Integer({minimum: 1, maximum: 2})}),
    async execute(_id, {page}) {
      const has_next = page === 1 && process.env.SASE_DEMO_MORE === "1";
      return {
        content: [{type: "text", text: JSON.stringify({page, has_next, row: "synthetic-row-private"})}],
        details: {has_next},
      };
    },
  });
}
