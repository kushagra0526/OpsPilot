// Live UI walkthrough of the Streamlit app (Groq model): runs all 12 scenarios through the real
// buttons, rejects one approval, reloads the page, and captures every screenshot the site uses.
// Start the app first on APP_URL with a freshly seeded runtime, then:
//   APP_URL=http://localhost:8504 node scripts/live-walkthrough.mjs
// Writes src/assets/screens/*.png and a machine-readable summary to ../evals/logs/walkthrough.json.
// Dry run against a demo-mode app: DRY_RUN=1 OUT_DIR=<scratch dir> (nothing in the repo is written).
// SHOTS_ONLY=1 re-captures only the approval and parallel-execution screenshots (scenarios 3 and 5)
// on a freshly seeded app, without touching the walkthrough summary.
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const SITE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DRY_RUN = process.env.DRY_RUN === "1";
const SHOTS_ONLY = process.env.SHOTS_ONLY === "1";
const SCREENS = DRY_RUN ? process.env.OUT_DIR : path.join(SITE, "src", "assets", "screens");
const SUMMARY = DRY_RUN ? path.join(SCREENS, "walkthrough.json") : path.resolve(SITE, "..", "evals", "logs", "walkthrough.json");
if (!SCREENS) throw new Error("DRY_RUN needs OUT_DIR");
const APP_URL = process.env.APP_URL ?? "http://localhost:8504";
const TURN_TIMEOUT = 300_000;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const consoleErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
const log = [];

const main = () => page.locator('[data-testid="stMain"]');
const messages = () => page.locator('[data-testid="stChatMessage"]');

async function settle() {
  await page.waitForFunction(() => !document.querySelector(".progress"), null, { timeout: TURN_TIMEOUT });
  await page.waitForTimeout(1200);
  await page.mouse.move(1430, 890);
}

const sessionInput = () => page.locator('[data-testid="stSidebar"] input[aria-label="Session"]');

async function newSession() {
  // Wait for the rerun to switch sessions and clear the chat, or the next turn is miscounted.
  const previous = await sessionInput().inputValue();
  await page.getByRole("button", { name: "New session" }).click();
  await page.waitForFunction(
    (prev) => {
      const input = document.querySelector('[data-testid="stSidebar"] input[aria-label="Session"]');
      return input && input.value !== prev && !document.querySelector('[data-testid="stChatMessage"]');
    },
    previous,
    { timeout: 60_000 },
  );
  await page.waitForTimeout(500);
}

async function runScenario(label, { fresh = true } = {}) {
  if (fresh) await newSession();
  const before = await messages().count();
  await page.getByRole("button", { name: label, exact: true }).click();
  await page.mouse.move(1430, 890);
  await page.waitForFunction((n) => document.querySelectorAll('[data-testid="stChatMessage"]').length >= n + 2, before, { timeout: TURN_TIMEOUT });
  await settle();
  const session = (await sessionInput().inputValue()).match(/sess-[0-9a-f]+/)?.[0] ?? null;
  const text = await main().innerText();
  const entry = {
    label,
    session,
    awaitingApproval: text.includes("Approval required."),
    hasExecution: /\+\s*Execution|-\s*Execution|Execution\n/.test(text) || text.includes("Raw step results"),
    refused: text.includes("I can only answer using NovaMart's internal knowledge"),
  };
  log.push(entry);
  return entry;
}

async function decide(choice, note) {
  // Every pending step gets the same decision (the current session shows only this turn's cards).
  const radios = page.getByText(choice, { exact: true });
  for (let i = 0; i < (await radios.count()); i++) await radios.nth(i).click();
  const notes = page.getByLabel("Note");
  for (let i = 0; i < (await notes.count()); i++) await notes.nth(i).fill(note);
  await page.getByRole("button", { name: "Submit decisions" }).click();
  await page.waitForTimeout(3000);
  await settle();
  const text = await main().innerText();
  log.push({
    label: `${choice} decision submitted`,
    stillWaiting: text.includes("Waiting for approval."),
  });
}

async function expand(name) {
  await page.locator("[data-testid=stExpander] summary").filter({ hasText: new RegExp(`${name}$`) }).last().click();
  await page.waitForTimeout(600);
}

async function scrollToLast(locator, block) {
  await locator.last().evaluate((el, b) => el.scrollIntoView({ block: b }), block);
  await page.waitForTimeout(800);
}

async function shoot(file, target = main()) {
  await page.mouse.move(1430, 890);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(1200);
  const dir = file.startsWith("data-tab") && !DRY_RUN ? path.resolve(SITE, "..", "evals", "evidence") : SCREENS;
  await target.screenshot({ path: path.join(dir, file), animations: "disabled" });
}

await mkdir(SCREENS, { recursive: true });
await page.goto(APP_URL);
await page.getByRole("button", { name: "New session" }).waitFor({ timeout: 120_000 });
const sidebar = await page.locator('[data-testid="stSidebar"]').innerText();
const model = ["Provider", "Large model", "Small model"]
  .map((k) => sidebar.match(new RegExp(`${k}:\\s*(\\S+)`))?.[0])
  .filter(Boolean)
  .join(" · ");
if (!DRY_RUN && !/groq/i.test(model)) throw new Error(`app is not in live Groq mode: ${model || sidebar.slice(0, 200)}`);

// Scenario 3: the plan pauses on the approval card, a person approves, the refund and then the email run.
async function largeRefund() {
  const s3 = await runScenario("3. Large refund needs approval");
  if (!s3.awaitingApproval) return false;
  await scrollToLast(page.getByText("Approval required.", { exact: false }), "center");
  await shoot("approval.png", page);
  await decide("Approve", "Photo of the dead pixel checked");
  await scrollToLast(page.locator("[data-testid=stExpander] summary").filter({ hasText: /Execution$/ }), "start");
  await shoot("approved-execution.png");
  return true;
}

// Scenario 5: coupon and ticket run in parallel; the email waits for both.
async function lateDelivery() {
  await runScenario("5. Late delivery, coupon + ticket in parallel");
  await shoot("parallel-execution.png");
}

if (SHOTS_ONLY) {
  await largeRefund();
  await lateDelivery();
  await writeFile(
    path.join(SCREENS, "CAPTURE.txt"),
    `Recaptured approval.png, approved-execution.png and parallel-execution.png ${new Date().toISOString()} from ${APP_URL} (live mode: ${model}); the other screenshots come from the full walkthrough.\n`,
    { flag: "a" },
  );
  await browser.close();
  console.log(JSON.stringify(log, null, 1));
  process.exit(consoleErrors.length ? 1 : 0);
}

// 0. Rejection path first: a cancellation that a person rejects must never execute.
const reject = await runScenario("9. Cancellation needs approval");
if (reject.awaitingApproval) await decide("Reject", "Customer changed their mind, keep the order");

// All 12 scenarios on one database (no reseed between them, unlike the eval harness), approving
// where the plan asks for it. Scenario 12 targets ORD-1007, which scenario 2 refunds, so it runs
// before scenario 2 to meet the order in its documented state.
await runScenario("1. Return window question");
await expand("Sources");
await shoot("sources-superseded.png");
await runScenario("12. Prompt injection blocked");
await shoot("injection-blocked.png");
await runScenario("2. Small refund auto-executed");
if (await largeRefund()) {
  // Reload: the persisted panels must come back for this session.
  await page.reload();
  await page.getByRole("button", { name: "New session" }).waitFor({ timeout: 120_000 });
  await settle();
  const restored = await main().innerText();
  log.push({ label: "page reload", panelsRestored: restored.includes("Process a full refund for ORD-1001") && /Execution/.test(restored) });
}
await runScenario("4. Out-of-window return, gold goodwill");
await lateDelivery();
await runScenario("6. Missing delivery date escalates");
await runScenario("7. Stock conflict on exchange");
await expand("Reasoning");
await shoot("conflict.png");
await runScenario("8. Out-of-scope refusal");
const s9 = await runScenario("9. Cancellation needs approval");
if (s9.awaitingApproval) await decide("Approve", "Customer confirmed the cancellation");
await runScenario("10A. Memory across sessions (store credit)");
await runScenario("10B. Memory across sessions (store credit)");
await runScenario("11. Grocery non-returnable");

// Tabs: memory and audit update; data tab renders.
await page.getByRole("tab", { name: "Memory" }).click();
await page.waitForTimeout(1500);
const memoryText = await main().innerText();
log.push({ label: "memory tab", storeCreditMemory: /store credit/i.test(memoryText) });
await shoot("memory.png");
await page.getByRole("tab", { name: "Audit log" }).click();
await page.waitForTimeout(2500);
await shoot("audit-log.png");
await page.getByRole("tab", { name: "Knowledge and data" }).click();
await page.waitForTimeout(1500);
// The data tab reads the live tables: the refunds the walkthrough approved must be there.
const table = page.locator('input[aria-label="Table"]');
await table.click();
await table.fill("refunds");
await page.keyboard.press("Enter");
await page.waitForTimeout(2500);
const evidenceDir = DRY_RUN ? SCREENS : path.resolve(SITE, "..", "evals", "evidence");
await mkdir(evidenceDir, { recursive: true });
await shoot("data-tab-refunds.png");
const rowsCaption = (await main().innerText()).match(/(\d+) rows/)?.[1];
log.push({ label: "data tab (refunds)", rendered: (await page.locator('[data-testid="stDataFrame"]').count()) > 0, rows: Number(rowsCaption ?? -1) });

await writeFile(path.join(SCREENS, "CAPTURE.txt"), `Captured ${new Date().toISOString()} from ${APP_URL} (${DRY_RUN ? "demo dry run" : "live mode"}: ${model})\n`);
await mkdir(path.dirname(SUMMARY), { recursive: true });
await writeFile(SUMMARY, JSON.stringify({ app: APP_URL, model, finished: new Date().toISOString(), steps: log, consoleErrors }, null, 2));
await browser.close();
console.log(JSON.stringify(log, null, 1));
if (consoleErrors.length) console.error("console errors:\n" + consoleErrors.join("\n"));
