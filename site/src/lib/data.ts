// Build-time readers for the repository's real artifacts. Nothing on the site restates these
// numbers by hand: evaluation tables, scenario prompts and outcomes, the README setup steps and
// the audit excerpt are all parsed from the files the agent's tooling writes.
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { marked } from "marked";
import { parse as parseYaml } from "yaml";

export const REPO = path.resolve(process.cwd(), "..");
const read = (rel: string) => readFileSync(path.join(REPO, rel), "utf8");
const exists = (rel: string) => existsSync(path.join(REPO, rel));

// ---------------------------------------------------------------- evaluation reports
export interface Table {
  head: string[];
  rows: string[][];
}
export interface ReportSection {
  title: string;
  meta: Record<string, string>;
  headline: string | null; // e.g. "Run 1: 12/12 scenarios fully passing"
  tables: Table[];
  perRun: string | null;
}
export interface Report {
  file: string;
  sections: ReportSection[];
}

const clean = (s: string) =>
  s
    .replace(/\u2705\s*/g, "")
    .replace(/\u274c\s*/g, "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .trim();

/** Pass/fail marks in the reports are emoji; the site renders words instead. */
const cell = (s: string) => {
  const t = s.trim();
  if (t === "\u2705") return "pass";
  if (t === "\u274c") return "fail";
  if (t === "\u2014") return "none";
  return clean(t).replace(/\u2014/g, "none");
};

function parseSection(title: string, lines: string[]): ReportSection {
  const meta: Record<string, string> = {};
  let headline: string | null = null;
  let perRun: string | null = null;
  const tables: Table[] = [];
  let current: Table | null = null;
  for (const raw of lines) {
    const line = raw.trim();
    if (line.startsWith("|")) {
      const cells = line.slice(1, -1).split("|");
      if (!current) current = { head: cells.map(cell), rows: [] };
      else if (!/^[-\s|:]+$/.test(line)) current.rows.push(cells.map(cell));
      continue;
    }
    if (current) {
      tables.push(current);
      current = null;
    }
    if (line.startsWith("- ")) {
      const body = clean(line.slice(2));
      if (/fully passing/i.test(body)) {
        headline = body;
        continue;
      }
      for (const part of body.split(" · ")) {
        const m = part.match(/^([A-Za-z ]+?):\s*(.+)$/) ?? part.match(/^(provider|large|small)\s+(.+)$/);
        if (m) meta[m[1].toLowerCase()] = m[2];
      }
    } else if (line.startsWith("**Per run:**")) {
      perRun = clean(line.replace("**Per run:**", ""));
    }
  }
  if (current) tables.push(current);
  return { title: clean(title), meta, headline, tables, perRun };
}

export function readReport(rel: string): Report | null {
  if (!exists(rel)) return null;
  const text = read(rel);
  const parts = text.split(/^## /m);
  const sections: ReportSection[] = [];
  if (parts.length === 1) {
    sections.push(parseSection(text.match(/^# (.+)$/m)?.[1] ?? rel, text.split("\n")));
  } else {
    for (const part of parts.slice(1)) {
      const [title, ...rest] = part.split("\n");
      sections.push(parseSection(title, rest));
    }
  }
  return { file: rel, sections };
}

export const REPORTS = {
  main: readReport("evals/results.md"),
  mainPartial: readReport("evals/results_live_partial.md"),
  mainDemo: readReport("evals/results_demo.md"),
  holdout: readReport("evals/holdout_results.md"),
  holdoutPartial: readReport("evals/holdout_results_partial.md"),
};

// ---------------------------------------------------------------- scenarios and outcomes
export interface Scenario {
  id: number | string;
  name: string;
  turns: { input: string; new_session?: boolean }[];
  expected_decision_type: string | string[];
  expected_tools: string[];
  must_require_approval: boolean;
  must_not_tools?: string[];
}
export const SCENARIOS = parseYaml(read("evals/scenarios.yaml")) as Scenario[];
export const HOLDOUT = parseYaml(read("evals/holdout.yaml")) as Scenario[];

export interface OutcomeStep {
  iteration: number;
  step_id: string;
  tool: string;
  status: string;
  summary: string;
}
export interface Outcome {
  id: number | string;
  name: string;
  prompts: string[];
  decision_type: string;
  iterations: number;
  approval_requested: boolean;
  approval_response?: { approved: boolean; approver?: string; note?: string } | null;
  steps: OutcomeStep[];
  conflicts: string[];
  final_response: string;
  passed: boolean;
}
export interface OutcomeFile {
  meta: { started: string; git: string; mode: string; model: Record<string, string> };
  scenarios: Outcome[];
}

const loadOutcomes = (rel: string): OutcomeFile | null =>
  exists(rel) ? (JSON.parse(read(rel)) as OutcomeFile) : null;

/** Live outcomes when a full live run exists; otherwise the scripted demo run, labelled as such. */
export const OUTCOMES = loadOutcomes("evals/outcomes_main.json") ?? loadOutcomes("evals/outcomes_main_demo.json");
export const OUTCOMES_ARE_LIVE = exists("evals/outcomes_main.json");

// ---------------------------------------------------------------- README and audit excerpt
/** The README "Quick start" section rendered to HTML, so the site matches it exactly. */
export function readmeSection(heading: string): string {
  const readme = read("README.md");
  const start = readme.indexOf(`## ${heading}\n`);
  if (start < 0) throw new Error(`README has no "## ${heading}" section`);
  const rest = readme.slice(start + heading.length + 4);
  const end = rest.search(/^## /m);
  return marked.parse(end < 0 ? rest : rest.slice(0, end), { async: false }) as string;
}

export interface AuditExcerpt {
  meta: { exported: string; mode: string; model: Record<string, string> };
  excerpts: {
    label: string;
    prompt: string;
    rows: { time: string; event: string; tool: string | null; risk: string | null; actor: string; rationale: string | null }[];
  }[];
}
export const AUDIT = JSON.parse(
  readFileSync(path.join(process.cwd(), "src", "data", "audit-excerpt.json"), "utf8"),
) as AuditExcerpt;

/** Policy rules from front matter, for quoting enforced numbers exactly. */
export function policyRules(): Record<string, unknown> {
  const dir = path.join(REPO, "data", "knowledge", "policies");
  const out: Record<string, unknown> = {};
  for (const f of ["refund_policy.md", "return_policy_v2.md", "loyalty_program.md", "shipping_policy.md"]) {
    const fm = readFileSync(path.join(dir, f), "utf8").split("---")[1];
    Object.assign(out, (parseYaml(fm) as { rules?: Record<string, unknown> }).rules ?? {});
  }
  return out;
}

/** "The suite has N tests" from the README, so the site and README can't disagree. */
export function readmeTestCount(): number {
  const m = read("README.md").match(/The suite has (\d+) tests/);
  if (!m) throw new Error("README no longer states the test count");
  return Number(m[1]);
}
