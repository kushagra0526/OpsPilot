import type { APIRoute } from "astro";
import { AUTHOR, PAGES } from "../lib/site";

// llms.txt: a plain-text summary of the project with links to the key pages.
export const GET: APIRoute = ({ site }) => {
  const url = (p: string) => new URL(p, site).href;
  const lines = [
    "# OpsPilot",
    "",
    "> A support-operations agent for NovaMart, a fictional store. A language model proposes actions from retrieved",
    "> policy, order, inventory and FAQ evidence; deterministic Python code enforces risk levels, human approval,",
    "> business preconditions and citation grounding before anything runs. Every action is written to an audit log.",
    "",
    `Author: ${AUTHOR.name} (${AUTHOR.github}, ${AUTHOR.linkedin})`,
    "",
    "## Pages",
    "",
    ...PAGES.map((p) => `- [${p.nav}](${url(p.path)}): ${p.description}`),
    "",
  ];
  return new Response(lines.join("\n"), { headers: { "Content-Type": "text/plain; charset=utf-8" } });
};
