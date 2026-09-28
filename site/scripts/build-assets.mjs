// Build-time assets, generated from single sources so nothing drifts:
//   brand/mark.svg          -> favicons, web manifest icons, Streamlit page icon/avatar
//   brand/fonts/*.woff2     -> site/public/fonts
//   docs/architecture.md    -> Mermaid diagrams rendered to static SVG (src/generated)
//   src/lib/pages.json + src/assets/screens -> one 1200x630 Open Graph image per page
import { copyFile, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import sharp from "sharp";
import { chromium } from "playwright";

const SITE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = path.resolve(SITE, "..");
const BRAND = path.join(ROOT, "brand");
const PUBLIC = path.join(SITE, "public");
const GENERATED = path.join(SITE, "src", "generated");
const SCREENS = path.join(SITE, "src", "assets", "screens");
const APP_STATIC = path.join(ROOT, "app", "static");

const PAPER = "#F6F4EF";
const INK = "#1C1B19";

async function icons() {
  const svg = await readFile(path.join(BRAND, "mark.svg"));
  await mkdir(PUBLIC, { recursive: true });
  await mkdir(APP_STATIC, { recursive: true });
  await writeFile(path.join(PUBLIC, "favicon.svg"), svg);
  await writeFile(path.join(APP_STATIC, "mark.svg"), svg);
  const render = (size, file) =>
    sharp(svg, { density: Math.ceil((72 * size) / 32) * 2 })
      .resize(size, size)
      .png({ compressionLevel: 9 })
      .toFile(file);
  await render(32, path.join(PUBLIC, "favicon-32.png"));
  await render(180, path.join(PUBLIC, "apple-touch-icon.png"));
  await render(192, path.join(PUBLIC, "icon-192.png"));
  await render(512, path.join(PUBLIC, "icon-512.png"));
  await render(64, path.join(APP_STATIC, "mark-64.png"));
  const operator = await readFile(path.join(BRAND, "operator.svg"));
  await sharp(operator, { density: 288 }).resize(64, 64).png().toFile(path.join(APP_STATIC, "operator-64.png"));
  const manifest = {
    name: "OpsPilot",
    short_name: "OpsPilot",
    description: "Support operations agent that proposes actions and lets code enforce the rules.",
    start_url: "/",
    display: "standalone",
    background_color: PAPER,
    theme_color: INK,
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
  await writeFile(path.join(PUBLIC, "site.webmanifest"), JSON.stringify(manifest, null, 2) + "\n");
}

async function fonts() {
  const dir = path.join(BRAND, "fonts");
  await mkdir(path.join(PUBLIC, "fonts"), { recursive: true });
  for (const f of await readdir(dir)) {
    await copyFile(path.join(dir, f), path.join(PUBLIC, "fonts", f));
  }
}

const FONT_CSS = (base) => `
@font-face { font-family: "Public Sans"; src: url("${base}/public-sans-latin-400-normal.woff2") format("woff2"); font-weight: 400; }
@font-face { font-family: "Public Sans"; src: url("${base}/public-sans-latin-600-normal.woff2") format("woff2"); font-weight: 600; }
@font-face { font-family: "Newsreader"; src: url("${base}/newsreader-latin-500-normal.woff2") format("woff2"); font-weight: 500; }
@font-face { font-family: "IBM Plex Mono"; src: url("${base}/ibm-plex-mono-latin-400-normal.woff2") format("woff2"); font-weight: 400; }
@font-face { font-family: "IBM Plex Mono"; src: url("${base}/ibm-plex-mono-latin-500-normal.woff2") format("woff2"); font-weight: 500; }
`;

async function scratchPage(browser, body, width, height) {
  const tmp = path.join(SITE, "scripts", ".render.html");
  const fontsUrl = pathToFileURL(path.join(BRAND, "fonts")).href;
  await writeFile(
    tmp,
    `<!doctype html><html><head><meta charset="utf-8"><style>${FONT_CSS(fontsUrl)}
     html,body{margin:0;background:${PAPER};color:${INK};font-family:"Public Sans",sans-serif}</style>
     </head><body>${body}</body></html>`,
  );
  const page = await browser.newPage({ viewport: { width, height } });
  await page.goto(pathToFileURL(tmp).href);
  await page.evaluate(() => document.fonts.ready);
  return page;
}

function mermaidBlocks(markdown) {
  return [...markdown.matchAll(/```mermaid\n([\s\S]*?)```/g)].map((m) => m[1].trim());
}

// Order matches docs/architecture.md: agent graph, knowledge/enforcement, approval sequence.
const DIAGRAMS = [
  { id: "agent-graph", title: "OpsPilot agent graph" },
  { id: "knowledge", title: "Knowledge sources and code enforcement" },
  { id: "approval-sequence", title: "Approval flow sequence" },
];

async function diagrams(browser) {
  const md = await readFile(path.join(ROOT, "docs", "architecture.md"), "utf8");
  const blocks = mermaidBlocks(md);
  if (blocks.length !== DIAGRAMS.length) {
    throw new Error(`expected ${DIAGRAMS.length} mermaid blocks in docs/architecture.md, found ${blocks.length}`);
  }
  const page = await scratchPage(browser, "", 1200, 900);
  await page.addScriptTag({ path: path.join(SITE, "node_modules", "mermaid", "dist", "mermaid.min.js") });
  await mkdir(GENERATED, { recursive: true });
  for (const [i, spec] of DIAGRAMS.entries()) {
    const svg = await page.evaluate(
      async ({ id, code }) => {
        const m = window.mermaid;
        m.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          htmlLabels: false,
          look: "classic",
          theme: "base",
          fontFamily: '"Public Sans", sans-serif',
          flowchart: { htmlLabels: false, curve: "linear", padding: 12 },
          sequence: { useMaxWidth: true, mirrorActors: false },
          themeVariables: {
            fontFamily: '"Public Sans", sans-serif',
            fontSize: "14px",
            background: "#F6F4EF",
            primaryColor: "#F6F4EF",
            primaryBorderColor: "#1C1B19",
            primaryTextColor: "#1C1B19",
            secondaryColor: "#EEEAE1",
            tertiaryColor: "#EEEAE1",
            lineColor: "#4A4740",
            textColor: "#1C1B19",
            clusterBkg: "#EEEAE1",
            clusterBorder: "#D9D3C6",
            edgeLabelBackground: "#F6F4EF",
            actorBkg: "#F6F4EF",
            actorBorder: "#1C1B19",
            actorTextColor: "#1C1B19",
            signalColor: "#1C1B19",
            signalTextColor: "#1C1B19",
            noteBkgColor: "#EEEAE1",
            noteBorderColor: "#D9D3C6",
            activationBkgColor: "#EEEAE1",
          },
        });
        const { svg } = await m.render(id, code);
        return svg;
      },
      { id: `d-${spec.id}`, code: blocks[i] },
    );
    const cleaned = svg
      .replace(/\sstyle="max-width:[^"]*"/, "")
      .replace(/\srole="[^"]*"/g, "")
      .replace(/\saria-roledescription="[^"]*"/g, "")
      .replace(/\swidth="100%"/, "")
      // Mermaid ships unused edge-animation CSS; the site has no motion, so drop it.
      .replace(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "")
      .replace(/[^{}]*\.edge-animation-(?:slow|fast)\{[^}]*\}/g, "")
      // No shadows or gradients anywhere on the site.
      .replace(/filter:\s*drop-shadow\([^;}]*\)\s*;?/g, "")
      .replace(/box-shadow:[^;}]*;?/g, "")
      .replace(/\sfilter="url\([^"]*\)"/g, "")
      .replace(/<filter\b[\s\S]*?<\/filter>/g, "")
      .replace(/<linearGradient\b[\s\S]*?<\/linearGradient>/g, "")
      .replace(/<defs><\/defs>/g, "")
      .replace(/\s(rx|ry)="([4-9]|\d{2,})(\.\d+)?"/g, ' $1="2"')
      // Mermaid hard-codes a few greys and pure black; map them onto the palette.
      .replace(/"#eaeaea"/gi, '"#EEEAE1"')
      .replace(/"#666"/g, '"#1C1B19"')
      .replace(/"#999"/g, '"#8C877C"')
      .replace(/#000000\b/g, "#1C1B19")
      .replace(/#000\b/g, "#1C1B19")
      .replace(/(["(:,\s])black\b/g, "$1#1C1B19")
      .replace(/<svg /, `<svg role="img" aria-labelledby="d-${spec.id}-title" `)
      .replace(/(<svg[^>]*>)/, `$1<title id="d-${spec.id}-title">${spec.title}</title>`);
    if (/<foreignObject|<br>/.test(cleaned)) throw new Error(`diagram ${spec.id} still has HTML labels`);
    if (/@keyframes|animation:/.test(cleaned)) throw new Error(`diagram ${spec.id} still has animation CSS`);
    if (/drop-shadow|box-shadow|<filter|Gradient/.test(cleaned)) {
      throw new Error(`diagram ${spec.id} still has a shadow or gradient`);
    }
    await writeFile(path.join(GENERATED, `diagram-${spec.id}.svg`), cleaned);
  }
  await page.close();
}

async function ogImages(browser) {
  const pages = JSON.parse(await readFile(path.join(SITE, "src", "lib", "pages.json"), "utf8"));
  const mark = await readFile(path.join(BRAND, "mark.svg"), "utf8");
  await mkdir(path.join(PUBLIC, "og"), { recursive: true });
  for (const p of pages.filter((p) => p.og)) {
    const shot = path.join(SCREENS, p.og.screen);
    if (!existsSync(shot)) throw new Error(`missing screenshot for OG image: ${shot}`);
    const body = `
      <div style="width:1200px;height:630px;box-sizing:border-box;padding:56px 64px;display:grid;
                  grid-template-columns:470px 1fr;gap:48px;border:1px solid #D9D3C6">
        <div style="display:flex;flex-direction:column;justify-content:space-between">
          <div style="display:flex;align-items:center;gap:14px">
            <div style="width:40px;height:40px">${mark.replace("<svg ", '<svg width="40" height="40" ')}</div>
            <div style="font-family:Newsreader;font-size:30px">OpsPilot</div>
          </div>
          <div>
            <div style="font-family:'IBM Plex Mono';font-size:18px;letter-spacing:.06em;text-transform:uppercase;
                        color:#2E5E4E;margin-bottom:18px">${p.og.kicker}</div>
            <div style="font-family:Newsreader;font-size:54px;line-height:1.08">${p.og.headline}</div>
          </div>
          <div style="font-size:19px;line-height:1.4;color:#4A4740">The model proposes. Code enforces risk,
            approvals and business rules.</div>
        </div>
        <div style="border:1px solid #1C1B19;overflow:hidden;align-self:center;height:518px">
          <img src="${pathToFileURL(shot).href}" alt="" style="display:block;height:100%;width:auto;
               object-fit:cover;object-position:${p.og.position ?? "left top"}">
        </div>
      </div>`;
    const page = await scratchPage(browser, body, 1200, 630);
    await page.waitForFunction(() => [...document.images].every((i) => i.complete && i.naturalWidth > 0));
    await page.screenshot({ path: path.join(PUBLIC, "og", `${p.slug || "home"}.png`), type: "png" });
    await page.close();
  }
}

await icons();
await fonts();
if (process.argv.includes("--icons-only")) {
  console.log("assets: icons and fonts generated");
  process.exit(0);
}
const browser = await chromium.launch();
try {
  await diagrams(browser);
  await ogImages(browser);
} finally {
  await browser.close();
}
console.log("assets: icons, fonts, diagrams, og images generated");
