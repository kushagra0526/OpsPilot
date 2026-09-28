// JSON-LD builders. All URLs derive from the configured SITE_URL (Astro `site`).
import { AUTHOR, absolute, type PageMeta } from "./site";

export interface Crumb {
  name: string;
  path: string;
}

export function jsonLd(site: URL, meta: PageMeta, crumbs: Crumb[]): object {
  const home = absolute(site, "/");
  const website = {
    "@type": "WebSite",
    "@id": `${home}#website`,
    url: home,
    name: "OpsPilot",
    description:
      "Project site for OpsPilot, a support-operations agent whose actions are checked by code before they run.",
    inLanguage: "en",
    author: { "@id": `${home}#author` },
  };
  const person = {
    "@type": "Person",
    "@id": `${home}#author`,
    name: AUTHOR.name,
    url: absolute(site, "/about"),
    sameAs: [AUTHOR.github, AUTHOR.linkedin],
  };
  const app = {
    "@type": "SoftwareApplication",
    "@id": `${home}#software`,
    name: "OpsPilot",
    url: home,
    applicationCategory: "BusinessApplication",
    operatingSystem: "Windows, macOS, Linux",
    softwareRequirements: "Python 3.11 or newer; a Groq API key for live mode",
    description:
      "An autonomous support-operations agent: an LLM proposes actions from retrieved policy and order data, and deterministic code enforces risk, approvals and business rules.",
    author: { "@id": `${home}#author` },
  };
  const graph: object[] = [website, person, app];
  if (crumbs.length > 1) {
    graph.push({
      "@type": "BreadcrumbList",
      "@id": `${absolute(site, meta.path)}#breadcrumb`,
      itemListElement: crumbs.map((c, i) => ({
        "@type": "ListItem",
        position: i + 1,
        name: c.name,
        item: absolute(site, c.path),
      })),
    });
  }
  return { "@context": "https://schema.org", "@graph": graph };
}
