// OpsPilot project site. Static output, near-zero client JavaScript.
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import { loadEnv } from "vite";

// SITE_URL is the single source for canonicals, sitemap, Open Graph and JSON-LD URLs.
const env = loadEnv(process.env.NODE_ENV ?? "production", process.cwd(), "");
const SITE_URL = (env.SITE_URL || "http://localhost:4321").replace(/\/+$/, "");

export default defineConfig({
  site: SITE_URL,
  output: "static",
  trailingSlash: "never",
  build: { format: "file", inlineStylesheets: "always" },
  compressHTML: true,
  integrations: [sitemap({ filter: (page) => !page.includes("/404") })],
  vite: { build: { sourcemap: false } },
  devToolbar: { enabled: false },
});
