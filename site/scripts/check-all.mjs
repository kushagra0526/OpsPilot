// Part C verification: builds the site, serves the production build locally, and runs every check.
// Writes site/QUALITY.md and exits non-zero if any check fails.
//   npm run check          (from site/)      or      make site-check      (from the repo root)
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { AxeBuilder } from "@axe-core/playwright";
import { LinkChecker } from "linkinator";
import lighthouse from "lighthouse";
import { parse as parseHtml } from "node-html-parser";
import { parse as parseYaml } from "yaml";
import sharp from "sharp";

const SITE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = path.resolve(SITE, "..");
const DIST = path.join(SITE, "dist");
const PORT = 4321;
const BASE = `http://localhost:${PORT}`;
const PAGES = JSON.parse(readFileSync(path.join(SITE, "src", "lib", "pages.json"), "utf8"));
const SITE_URL = (readEnv().SITE_URL ?? BASE).replace(/\/+$/, "");
const results = [];
const lighthouseRows = [];

function readEnv() {
  const file = path.join(SITE, ".env");
  if (!existsSync(file)) return {};
  return Object.fromEntries(
    readFileSync(file, "utf8")
      .split(/\r?\n/)
      .filter((l) => l.includes("=") && !l.trim().startsWith("#"))
      .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]),
  );
}

function record(name, pass, details = []) {
  results.push({ name, pass, details });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${details.length ? "\n      " + details.slice(0, 25).join("\n      ") : ""}`);
}

function walk(dir, filter = () => true) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((f) => {
    const p = path.join(dir, f);
    if (statSync(p).isDirectory()) return f === "node_modules" || f === "static" && dir.endsWith("fonts") ? [] : walk(p, filter);
    return filter(p) ? [p] : [];
  });
}

const htmlFiles = () => walk(DIST, (p) => p.endsWith(".html"));
const routeOf = (file) => {
  const rel = path.relative(DIST, file).replace(/\\/g, "/").replace(/\.html$/, "");
  return rel === "index" ? "/" : `/${rel}`;
};

// 1. Build ------------------------------------------------------------------------------------
function build() {
  const out = spawnSync("npm", ["run", "build"], { cwd: SITE, shell: true, encoding: "utf8" });
  const text = (out.stdout ?? "") + (out.stderr ?? "");
  const warnings = text.split(/\r?\n/).filter((l) => /\bwarn(ing)?\b/i.test(l) && !/0 warnings/i.test(l));
  record("1a. Build succeeds", out.status === 0, out.status === 0 ? [] : text.split("\n").slice(-20));
  record("1b. Build has zero warnings", warnings.length === 0, warnings);
  const maps = walk(DIST, (p) => p.endsWith(".map"));
  record("1c. No .map files in dist/", maps.length === 0, maps.map((m) => path.relative(SITE, m)));
  return out.status === 0;
}

// 2. Serve the build ---------------------------------------------------------------------------
async function serve() {
  const proc = spawn("npx", ["astro", "preview", "--port", String(PORT)], { cwd: SITE, shell: true });
  let log = "";
  proc.stdout.on("data", (d) => (log += d));
  proc.stderr.on("data", (d) => (log += d));
  for (let i = 0; i < 180; i++) {
    if (proc.exitCode !== null) throw new Error(`preview server exited:\n${log}`);
    try {
      const r = await fetch(BASE + "/");
      if (r.ok) return proc;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`preview server did not start:
${log}`);
}

// 3-4. Browser pass: console, failed requests, images, axe --------------------------------------
async function browserChecks() {
  const browser = await chromium.launch();
  const problems = [];
  const axeProblems = [];
  const routes = [...PAGES.map((p) => p.path), "/this-address-does-not-exist"];
  for (const route of routes) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    const expect404 = route.startsWith("/this-address");
    page.on("console", (m) => {
      if (m.type() === "error" || m.type() === "warning") {
        if (expect404 && /status of 404/.test(m.text())) return;
        problems.push(`${route}: console ${m.type()}: ${m.text()}`);
      }
    });
    page.on("requestfailed", (r) => problems.push(`${route}: request failed ${r.url()}`));
    page.on("response", (r) => {
      const isDoc = r.url().replace(/\/$/, "") === (BASE + route).replace(/\/$/, "");
      if (r.status() >= 400 && !(expect404 && isDoc)) problems.push(`${route}: HTTP ${r.status()} ${r.url()}`);
    });
    const resp = await page.goto(BASE + route, { waitUntil: "networkidle" });
    if (expect404 && resp.status() !== 404) problems.push(`${route}: expected HTTP 404, got ${resp.status()}`);
    await page.evaluate(async () => {
      for (const img of document.images) img.loading = "eager";
      await Promise.all([...document.images].map((i) => (i.complete ? null : new Promise((r) => (i.onload = i.onerror = r)))));
    });
    const broken = await page.evaluate(() => [...document.images].filter((i) => !i.naturalWidth).map((i) => i.currentSrc || i.src));
    broken.forEach((src) => problems.push(`${route}: image failed to load ${src}`));
    const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
    for (const v of axe.violations) axeProblems.push(`${route}: ${v.id} (${v.impact}) x${v.nodes.length}: ${v.help}`);
    await context.close();
  }
  await browser.close();
  record("2. Playwright: every page and the 404, no console errors/warnings, failed requests or missing images", problems.length === 0, problems);
  record("5. axe (WCAG 2.2 AA + best practices): zero violations", axeProblems.length === 0, axeProblems);
}

// 4. Links -----------------------------------------------------------------------------------
async function links() {
  const checker = new LinkChecker();
  const res = await checker.check({
    path: BASE + "/",
    recurse: true,
    retry: true,
    retryErrors: true,
    retryErrorsCount: 3,
    timeout: 20000,
    concurrency: 4,
    userAgent: "Mozilla/5.0 (link check for the OpsPilot project site)",
  });
  const bad = res.links.filter((l) => {
    if (l.state !== "BROKEN") return false;
    // LinkedIn answers automated clients with HTTP 999 even for valid public profiles.
    if (/(^|\.)linkedin\.com\//.test(l.url.replace(/^https?:\/\//, "")) && l.status === 999) return false;
    return true;
  });
  const internal = res.links.filter((l) => l.url.startsWith(BASE)).length;
  const external = res.links.length - internal;
  const linkedin = res.links.filter((l) => /linkedin\.com/.test(l.url)).map((l) => `${l.url} -> ${l.status}`);
  record(
    `3. Links: ${internal} internal and ${external} external checked, zero broken`,
    bad.length === 0,
    bad.map((l) => `${l.status} ${l.url} (from ${l.parent})`).concat(bad.length ? [] : linkedin.map((l) => `note: ${l}`)),
  );
}

// 5. Lighthouse --------------------------------------------------------------------------------
async function lighthouseChecks() {
  // Playwright launches Chromium with a debugging port; Lighthouse drives it over CDP.
  const debugPort = 9333;
  const chrome = await chromium.launch({ args: [`--remote-debugging-port=${debugPort}`] });
  const problems = [];
  try {
    const routes = [...PAGES.map((p) => p.path), "/404"];
    // Warm the local preview server so its first cold response does not skew the first audit.
    for (const route of routes) await fetch(BASE + route).then((r) => r.arrayBuffer());
    for (const route of routes) {
      const r = await lighthouse(BASE + route, {
        port: debugPort,
        output: "json",
        logLevel: "error",
        onlyCategories: ["performance", "accessibility", "best-practices", "seo"],
      });
      const c = r.lhr.categories;
      const row = {
        route,
        performance: Math.round(c.performance.score * 100),
        accessibility: Math.round(c.accessibility.score * 100),
        bestPractices: Math.round(c["best-practices"].score * 100),
        seo: Math.round(c.seo.score * 100),
        failedSeo: Object.values(r.lhr.audits)
          .filter((a) => r.lhr.categories.seo.auditRefs.some((ref) => ref.id === a.id && ref.weight > 0) && a.score !== null && a.score < 1)
          .map((a) => a.id),
      };
      lighthouseRows.push(row);
      console.log(`  lighthouse ${route}: perf ${row.performance} a11y ${row.accessibility} bp ${row.bestPractices} seo ${row.seo}${row.failedSeo.length ? " (" + row.failedSeo.join(",") + ")" : ""}`);
      const is404 = route === "/404";
      const ok =
        row.performance >= 95 &&
        row.accessibility === 100 &&
        row.bestPractices === 100 &&
        (is404 ? row.failedSeo.every((id) => id === "is-crawlable") : row.seo === 100);
      if (!ok) problems.push(`${route}: perf ${row.performance}, a11y ${row.accessibility}, bp ${row.bestPractices}, seo ${row.seo} ${row.failedSeo.join(",")}`);
    }
  } finally {
    await chrome.close();
  }
  record("4. Lighthouse mobile: Performance >= 95, Accessibility 100, Best Practices 100, SEO 100", problems.length === 0, problems);
}

// 6. HTML validation ---------------------------------------------------------------------------
function htmlValidate() {
  const out = spawnSync("npx", ["html-validate", "dist/**/*.html"], { cwd: SITE, shell: true, encoding: "utf8" });
  const text = ((out.stdout ?? "") + (out.stderr ?? "")).trim();
  record("6. html-validate: zero errors", out.status === 0, out.status === 0 ? [] : text.split("\n").slice(0, 40));
}

// 7. Structure: headings, titles, descriptions, canonicals, OG images, alt text, JSON-LD --------
async function structure() {
  const problems = [];
  const titles = new Map();
  const descs = new Map();
  for (const file of htmlFiles()) {
    const route = routeOf(file);
    const root = parseHtml(readFileSync(file, "utf8"));
    const is404 = route === "/404";
    const h1 = root.querySelectorAll("h1");
    if (h1.length !== 1) problems.push(`${route}: ${h1.length} h1 elements`);
    let last = 1;
    for (const h of root.querySelectorAll("main h1, main h2, main h3, main h4")) {
      const level = Number(h.tagName[1]);
      if (level > last + 1) problems.push(`${route}: heading jumps from h${last} to h${level} ("${h.text.trim()}")`);
      last = level;
    }
    const title = root.querySelector("title")?.text.trim();
    const desc = root.querySelector('meta[name="description"]')?.getAttribute("content");
    if (!title) problems.push(`${route}: no title`);
    if (!desc) problems.push(`${route}: no meta description`);
    if (titles.has(title)) problems.push(`${route}: title duplicates ${titles.get(title)}`);
    if (descs.has(desc)) problems.push(`${route}: description duplicates ${descs.get(desc)}`);
    titles.set(title, route);
    descs.set(desc, route);
    const canonical = root.querySelector('link[rel="canonical"]')?.getAttribute("href");
    if (!is404) {
      const want = route === "/" ? `${SITE_URL}/` : `${SITE_URL}${route}`;
      if (canonical !== want) problems.push(`${route}: canonical ${canonical} (expected ${want})`);
    } else if (!root.querySelector('meta[name="robots"][content="noindex"]')) {
      problems.push("/404: missing noindex");
    }
    const og = root.querySelector('meta[property="og:image"]')?.getAttribute("content");
    if (!og || !og.startsWith(SITE_URL)) problems.push(`${route}: og:image missing or not on SITE_URL (${og})`);
    else {
      const local = path.join(DIST, og.slice(SITE_URL.length));
      if (!existsSync(local)) problems.push(`${route}: og:image file missing ${local}`);
      else {
        const meta = await sharp(local).metadata();
        if (meta.width !== 1200 || meta.height !== 630) problems.push(`${route}: og:image is ${meta.width}x${meta.height}`);
      }
    }
    for (const tag of ["og:title", "og:description", "og:url"]) {
      if (!root.querySelector(`meta[property="${tag}"]`)) problems.push(`${route}: missing ${tag}`);
    }
    if (!root.querySelector('meta[name="twitter:card"][content="summary_large_image"]')) problems.push(`${route}: no twitter card`);
    for (const img of root.querySelectorAll("img")) {
      if (img.getAttribute("alt") === undefined) problems.push(`${route}: img without alt ${img.getAttribute("src")}`);
    }
    // JSON-LD
    const blocks = root.querySelectorAll('script[type="application/ld+json"]');
    if (blocks.length !== 1) problems.push(`${route}: ${blocks.length} JSON-LD blocks`);
    for (const b of blocks) {
      let data;
      try {
        data = JSON.parse(b.text);
      } catch (e) {
        problems.push(`${route}: JSON-LD does not parse: ${e.message}`);
        continue;
      }
      if (data["@context"] !== "https://schema.org") problems.push(`${route}: JSON-LD @context`);
      const byType = Object.fromEntries((data["@graph"] ?? []).map((n) => [n["@type"], n]));
      const need = { WebSite: ["name", "url"], Person: ["name", "url", "sameAs"], SoftwareApplication: ["name", "applicationCategory", "operatingSystem", "description"] };
      for (const [type, props] of Object.entries(need)) {
        if (!byType[type]) problems.push(`${route}: JSON-LD missing ${type}`);
        else for (const p of props) if (!byType[type][p]) problems.push(`${route}: ${type}.${p} missing`);
      }
      if (byType.LocalBusiness) problems.push(`${route}: LocalBusiness must not be present`);
      for (const node of data["@graph"] ?? []) {
        for (const [k, v] of Object.entries(node)) {
          if (typeof v === "string" && /^https?:/.test(v) && k !== "sameAs" && v.includes("localhost") !== SITE_URL.includes("localhost")) {
            problems.push(`${route}: ${node["@type"]}.${k} not on SITE_URL`);
          }
        }
      }
      const crumbs = root.querySelectorAll('nav[aria-label="Breadcrumb"] li');
      const list = byType.BreadcrumbList;
      if (route === "/" || is404) {
        if (list || crumbs.length) problems.push(`${route}: should have no breadcrumbs`);
      } else {
        if (!list || !crumbs.length) problems.push(`${route}: breadcrumbs or BreadcrumbList missing`);
        else {
          const items = list.itemListElement;
          if (items.length !== crumbs.length) problems.push(`${route}: BreadcrumbList has ${items.length} items, page shows ${crumbs.length}`);
          items.forEach((it, i) => {
            const shown = crumbs[i]?.text.trim();
            if (it.position !== i + 1 || it.name !== shown) problems.push(`${route}: breadcrumb ${i + 1} "${it.name}" vs shown "${shown}"`);
            const a = crumbs[i]?.querySelector("a");
            const href = a ? new URL(a.getAttribute("href"), SITE_URL + "/").href : `${SITE_URL}${route}`;
            if (it.item !== href) problems.push(`${route}: breadcrumb ${i + 1} item ${it.item} vs ${href}`);
          });
        }
      }
    }
  }
  const sitemapOk = existsSync(path.join(DIST, "sitemap-index.xml"));
  const robots = existsSync(path.join(DIST, "robots.txt")) ? readFileSync(path.join(DIST, "robots.txt"), "utf8") : "";
  if (!sitemapOk) problems.push("sitemap-index.xml missing");
  if (!robots.includes(`Sitemap: ${SITE_URL}/sitemap-index.xml`)) problems.push("robots.txt does not point to the sitemap");
  const llms = existsSync(path.join(DIST, "llms.txt")) ? readFileSync(path.join(DIST, "llms.txt"), "utf8") : "";
  for (const p of PAGES) if (!llms.includes(`${SITE_URL}${p.path === "/" ? "/" : p.path}`)) problems.push(`llms.txt does not link ${p.path}`);
  for (const f of ["favicon.svg", "favicon-32.png", "apple-touch-icon.png", "site.webmanifest"]) {
    if (!existsSync(path.join(DIST, f))) problems.push(`missing ${f}`);
  }
  record("7. Structure: one h1, heading order, unique titles/descriptions, canonicals, OG images, alt, JSON-LD, breadcrumbs, sitemap, robots, llms.txt, icons", problems.length === 0, problems);
}

// 8. Banned content grep -----------------------------------------------------------------------
function grep() {
  const textExt = /\.(astro|ts|mjs|js|css|md|html|xml|txt|json|svg|py|toml|webmanifest)$/;
  const files = [
    ...walk(path.join(SITE, "src"), (p) => textExt.test(p)),
    ...walk(DIST, (p) => textExt.test(p)),
    ...walk(path.join(ROOT, "app"), (p) => textExt.test(p)),
    path.join(ROOT, ".streamlit", "config.toml"),
    path.join(ROOT, "README.md"),
  ];
  const patterns = [
    ["em dash", /—/],
    ["emoji", /\p{Extended_Pictographic}/u],
    ["lorem", /lorem/i],
    ["TODO", /TODO/],
    ["coming soon", /coming soon/i],
    ['href="#"', /href="#"/],
    ["banned font", /\b(Inter|Geist|Space Grotesk|Poppins)\b/],
    ["lucide", /lucide/i],
  ];
  const hits = [];
  for (const f of files) {
    const text = readFileSync(f, "utf8");
    text.split("\n").forEach((line, i) => {
      for (const [name, re] of patterns) if (re.test(line)) hits.push(`${name}: ${path.relative(ROOT, f)}:${i + 1}: ${line.trim().slice(0, 100)}`);
    });
  }
  record(`8. Banned content in ${files.length} files (site source, dist, Streamlit app, README): zero matches`, hits.length === 0, hits);
}

// 9. Numbers on the site match the eval reports and the README ---------------------------------
function numbers() {
  const problems = [];
  const text = (route) => parseHtml(readFileSync(path.join(DIST, route === "/" ? "index.html" : `${route.slice(1)}.html`), "utf8")).text.replace(/\s+/g, " ");
  const evalText = text("/evaluation");
  const reportFile = existsSync(path.join(ROOT, "evals", "results.md")) ? "evals/results.md" : "evals/results_live_partial.md";
  const holdoutFile = existsSync(path.join(ROOT, "evals", "holdout_results.md")) ? "evals/holdout_results.md" : "evals/holdout_results_partial.md";
  for (const rel of [reportFile, holdoutFile, "evals/results_demo.md"]) {
    const md = readFileSync(path.join(ROOT, rel), "utf8");
    const headline = md.match(/(Scenarios fully passing: \d+\/\d+|Run 1: \d+\/\d+ scenarios fully passing)/)?.[1];
    if (headline && !evalText.includes(headline)) problems.push(`evaluation page lacks "${headline}" from ${rel}`);
    // Per-scenario result tables have a "Checks" column; the stability table has one mark per run,
    // which the site renders as words, so only its "Passed" count (e.g. 3/3) is compared.
    let head = [];
    for (const line of rel.endsWith("demo.md") ? [] : md.split("\n")) {
      const cells = line.split("|").map((c) => c.trim());
      if (/^\| # \|/.test(line)) head = cells;
      if (!/^\| (\d+|H\d+) \|/.test(line)) continue;
      const id = cells[1];
      const values = head.includes("Checks")
        ? [cells[2], cells[3], cells[5], cells[6]]
        : [cells[2], cells[head.indexOf("Passed")]];
      for (const v of values) if (v && !evalText.includes(v)) problems.push(`evaluation page lacks ${id} value "${v}" from ${rel}`);
    }
  }
  const readme = readFileSync(path.join(ROOT, "README.md"), "utf8");
  const tests = readme.match(/The suite has (\d+) tests/)[1];
  // Adjacent spans ("143" + "offline tests") have no whitespace between them in textContent.
  if (!new RegExp(`${tests}\\s*offline tests`).test(text("/"))) problems.push(`home page test count differs from README (${tests})`);
  const scenarios = parseYaml(readFileSync(path.join(ROOT, "evals", "scenarios.yaml"), "utf8"));
  if (!new RegExp(`${scenarios.length}\\s*demo scenarios`).test(text("/"))) problems.push("home page scenario count differs");
  const scenText = text("/scenarios");
  for (const sc of scenarios) for (const t of sc.turns) if (!scenText.includes(t.input)) problems.push(`scenarios page lacks prompt of ${sc.id}`);
  const quick = readme.split("## Quick start")[1].split("\n## ")[0];
  const gs = text("/getting-started");
  // Commands from fenced blocks (line by line) and from inline code spans outside them.
  const fenced = [...quick.matchAll(/```\w*\n([\s\S]*?)```/g)].flatMap((m) => m[1].split("\n").filter(Boolean));
  const inline = (quick.replace(/```[\s\S]*?```/g, "").match(/`[^`\n]+`/g) ?? []).map((c) => c.slice(1, -1));
  for (const cmd of [...fenced, ...inline]) {
    if (!gs.includes(cmd.trim())) problems.push(`getting-started lacks README command ${cmd}`);
  }
  const rules = ["refund_policy.md"].map((f) => readFileSync(path.join(ROOT, "data", "knowledge", "policies", f), "utf8"))[0];
  const limit = rules.match(/auto_refund_limit: (\d+)/)[1];
  if (!text("/safety").includes(`Refund above $${limit}`)) problems.push("safety page refund limit differs from policy front matter");
  record("9. Every number on the site matches evals/*.md, the README and the policy files", problems.length === 0, problems);
}

// 10. Agent test suite -------------------------------------------------------------------------
function agentTests() {
  const py = process.platform === "win32" ? path.join(ROOT, ".venv", "Scripts", "python.exe") : path.join(ROOT, ".venv", "bin", "python");
  const out = spawnSync(py, ["-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider"], { cwd: ROOT, encoding: "utf8" });
  const lines = ((out.stdout ?? "") + (out.stderr ?? "")).split("\n");
  const summary = lines.filter((l) => /passed|failed|error/.test(l)).slice(-3);
  const failures = lines.filter((l) => /^(FAILED|E {2})/.test(l)).slice(0, 12);
  record(`10. make test (agent): ${summary.at(-1) ?? "no summary"}`, out.status === 0, out.status === 0 ? [] : [...summary, ...failures]);
}

function writeQuality() {
  const lines = [
    "# Site quality report",
    "",
    `Generated ${new Date().toISOString().slice(0, 19).replace("T", " ")} UTC by \`scripts/check-all.mjs\` against the local production build (\`astro preview\`, ${BASE}). SITE_URL: ${SITE_URL}.`,
    "",
    "## Lighthouse (mobile)",
    "",
    "| Page | Performance | Accessibility | Best Practices | SEO |",
    "|---|---|---|---|---|",
    ...lighthouseRows.map(
      (r) =>
        `| ${r.route} | ${r.performance} | ${r.accessibility} | ${r.bestPractices} | ${r.seo}${r.route === "/404" && r.seo < 100 ? " (noindex by design)" : ""} |`,
    ),
    "",
    "The 404 page is served with `noindex`, so Lighthouse's crawlability audit fails there by design; an error page must not be indexed.",
    "",
    "## Checks",
    "",
    "| Check | Result |",
    "|---|---|",
    ...[...results].sort((a, b) => parseFloat(a.name) - parseFloat(b.name) || a.name.localeCompare(b.name)).map((r) => `| ${r.name.replace(/\|/g, "/")} | ${r.pass ? "pass" : "fail"} |`),
    "",
  ];
  const failing = results.filter((r) => !r.pass);
  if (failing.length) {
    lines.push("## Failures", "");
    for (const f of failing) lines.push(`- ${f.name}`, ...f.details.slice(0, 20).map((d) => `  - ${d}`));
    lines.push("");
  }
  writeFileSync(path.join(SITE, "QUALITY.md"), lines.join("\n"));
}

const only = process.argv.slice(2);
const want = (n) => only.length === 0 || only.includes(n);
let server;
try {
  if (want("build") && !build()) throw new Error("build failed");
  server = await serve();
  if (want("browser")) await browserChecks();
  if (want("links")) await links();
  if (want("lighthouse")) await lighthouseChecks();
  if (want("html")) htmlValidate();
  if (want("structure")) await structure();
  if (want("grep")) grep();
  if (want("numbers")) numbers();
  if (want("tests")) agentTests();
} finally {
  if (server) {
    if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(server.pid), "/T", "/F"]);
    else server.kill();
  }
}
if (only.length === 0) writeQuality();
const failed = results.filter((r) => !r.pass).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
