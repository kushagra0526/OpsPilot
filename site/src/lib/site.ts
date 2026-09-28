// Site-wide constants and page registry. SITE_URL comes from astro.config (site/.env).
import pages from "./pages.json";

export interface PageMeta {
  slug: string;
  path: string;
  nav: string;
  title: string;
  description: string;
  footerOnly?: boolean;
  og?: { kicker: string; headline: string; screen: string; position?: string };
}

export const PAGES = pages as PageMeta[];

export const AUTHOR = {
  name: "Kushagra Pratap Singh",
  github: "https://github.com/kushagra0526",
  linkedin: "https://www.linkedin.com/in/kushagra-pratap-singh/",
};

export function page(path: string): PageMeta {
  const found = PAGES.find((p) => p.path === path);
  if (!found) throw new Error(`no page registered for ${path}`);
  return found;
}

/** Absolute URL on the configured SITE_URL origin (no trailing slash except the root). */
export function absolute(site: URL | undefined, path: string): string {
  if (!site) throw new Error("astro `site` (SITE_URL) is not configured");
  return new URL(path, site).href;
}
