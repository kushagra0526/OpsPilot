# Site quality report

Generated 2026-09-27 18:25:18 UTC by `scripts/check-all.mjs` against the local production build (`astro preview`, http://localhost:4321). SITE_URL: http://localhost:4321.

## Lighthouse (mobile)

| Page | Performance | Accessibility | Best Practices | SEO |
|---|---|---|---|---|
| / | 100 | 100 | 100 | 100 |
| /how-it-works | 100 | 100 | 100 | 100 |
| /safety | 100 | 100 | 100 | 100 |
| /evaluation | 100 | 100 | 100 | 100 |
| /scenarios | 100 | 100 | 100 | 100 |
| /getting-started | 100 | 100 | 100 | 100 |
| /about | 100 | 100 | 100 | 100 |
| /privacy | 100 | 100 | 100 | 100 |
| /terms | 100 | 100 | 100 | 100 |
| /404 | 100 | 100 | 100 | 63 (noindex by design) |

The 404 page is served with `noindex`, so Lighthouse's crawlability audit fails there by design; an error page must not be indexed.

## Checks

| Check | Result |
|---|---|
| 1a. Build succeeds | pass |
| 1b. Build has zero warnings | pass |
| 1c. No .map files in dist/ | pass |
| 2. Playwright: every page and the 404, no console errors/warnings, failed requests or missing images | pass |
| 3. Links: 60 internal and 13 external checked, zero broken | pass |
| 4. Lighthouse mobile: Performance >= 95, Accessibility 100, Best Practices 100, SEO 100 | pass |
| 5. axe (WCAG 2.2 AA + best practices): zero violations | pass |
| 6. html-validate: zero errors | pass |
| 7. Structure: one h1, heading order, unique titles/descriptions, canonicals, OG images, alt, JSON-LD, breadcrumbs, sitemap, robots, llms.txt, icons | pass |
| 8. Banned content in 47 files (site source, dist, Streamlit app, README): zero matches | pass |
| 9. Every number on the site matches evals/*.md, the README and the policy files | pass |
| 10. make test (agent): 148 passed in 172.12s (0:02:52) | pass |
