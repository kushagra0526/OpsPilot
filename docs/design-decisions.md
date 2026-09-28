# Design decisions

One line each: decision and reason, in the order they were made. Early entries mention a scripted demo mode; it was later removed from the app and CLI, which always use the live model. The scripted planner survives only as an offline test double in `tests/`.

## Setup and API verification
- Python 3.13 venv (3.14 is the system default) — best wheel coverage for chromadb/onnxruntime.
- Package installed editable from `src/` via `pyproject.toml` (`pip install -e . --no-deps`) — scripts, app, CLI and tests import `opspilot` without path hacks.
- Makefile branches on `$(OS)` for venv paths; on Windows run it with `mingw32-make` — GNU make isn't on PATH here.
- LangGraph 1.2.12 verified: `from langgraph.types import interrupt, Command`; `interrupt(value)` pauses and the node re-runs on resume, `graph.invoke(Command(resume=...), config)` resumes, `graph.get_state(config).interrupts` holds the payload — so audit writes happen in a node *before* the interrupting node to avoid duplicates on re-run.
- Checkpointer: `from langgraph.checkpoint.sqlite import SqliteSaver`; `SqliteSaver(sqlite3.connect(path, check_same_thread=False))` (langgraph-checkpoint-sqlite 3.1.1).
- Chroma 1.5.9: collection names need ≥3 chars; cosine space set with `configuration={"hnsw": {"space": "cosine"}}`; `DefaultEmbeddingFunction` downloads ONNX all-MiniLM-L6-v2 once into `~/.cache/chroma` (no API key).
- Groq models: **current models are `openai/gpt-oss-120b` (large) / `openai/gpt-oss-20b` (small)** — see "Live validation". (Initially no key was available, so the the brief's `llama-3.3-70b-versatile` / `llama-3.1-8b-instant` were kept unverified; once checked they turned out to be retired on Groq.)
- LLM calls use plain chat completion with Groq JSON mode (`response_format=json_object`) plus our own fence-stripping Pydantic parser — identical code path for FakeLLM and live models, and one retry with the validation error appended.
- Gemini fallback model defaults to `gemini-flash-latest` via `OPSPILOT_GEMINI_MODEL` (2.5/2.0 Flash are retired) — only used when `GOOGLE_API_KEY` is set.

## Data
- Seed logic lives in `src/opspilot/seeding.py` (script is a thin wrapper) — the UI "Reset demo data" button and tests reuse it.
- Seed drops/recreates tables inside the existing DB file instead of deleting it — Windows keeps file locks while the UI holds connections.
- `coupons.amount_credit REAL NULL` added — store-credit refunds create a coupon with `percent_off=0` and a money value, plus a normal `refunds` row (method `store_credit`) so the refundable remaining stays correct.
- Background orders use IDs `ORD-2001..2060` and non-demo customers only — demo records stay untouched and unambiguous.
- ORD-1003's item is `AP-SNK-12` Runner Sneakers ($79), ORD-1008's is `EL-HP-02` — the brief leaves these open.
- All non-conflicting DB stock rows are stamped `2026-09-25T08:00` (fresher than the CSV's `2026-09-15`) so exactly one real conflict exists (EL-HP-02).
- Policy chunk text is prefixed with `Title vN §S Section title:` — improves retrieval and makes evidence self-describing.
- FAQ is a separate Chroma collection `faq` (semantic search) — one search mechanism for both unstructured sources.
- `.gitattributes` forces LF — keeps Makefile recipes and scripts portable across Windows/Linux clones.

## Retrieval
- Policy search = top-6 over `[user request, intent summary]` plus top-2 per deterministic "hint" query derived from the referenced orders (item categories, undelivered, missing delivery date), merged and capped at 10 chunks — the planner reliably sees the rules that apply to the actual items.
- When a superseded chunk is retrieved, the superseding doc is searched directly (top-2) if it isn't already present — the agent always sees the current rule next to the old one.
- Supersession is computed from Chroma metadata (`supersedes` + `effective_date ≤ REFERENCE_DATE`), not hard-coded.
- Retrieval runs in two parallel waves: (1) orders + explicit customers, (2) policies, FAQ, order owners and inventory — policy hints and SKUs depend on wave 1.
- Order owners are always pulled in as customer evidence — tier, email and refund preference are needed for goodwill, emails and refunds.
- Product names in the text (e.g. "Nova Buds Pro") are mapped to SKUs by exact name match — scenario 1 has no SKU but needs the category.
- Relevance threshold = cosine similarity 0.20 (measured: off-domain prompts ≈0.08, weakest in-domain ≈0.24, FAQ questions ≈0.75); refusal requires both no entity matches and max similarity below it.
- Inventory evidence is two items (`db:products:SKU`, `csv:inventory:SKU`) — both values stay visible and citable; the conflict note names the fresher source.

## Tools and safety
- `issue_coupon` has a `purpose` arg (`goodwill` | `shipping_delay`, + `order_id`) — standard-tier goodwill limit is 0%, but the shipping policy grants a 10% delay coupon to any tier; the risk policy applies tier limits to goodwill only, and a precondition verifies the order really is >10 days late for `shipping_delay`.
- `issue_refund` has optional `skus` (partial refunds) — eligibility is checked per refunded item's category, and the amount can't exceed those items' value.
- `issue_refund.method` is optional; code resolves it to the customer's `preferred_refund_method` (learned from memory) or `original` — preference is applied deterministically even if the model forgets.
- Return windows (electronics 14, apparel/home 30, grocery non-returnable) are code constants mirroring Return Policy v2 — preconditions must be deterministic; the LLM still reasons over the policy text.
- Opened-vs-defective electronics condition is not enforced in code — there's no structured condition data; it stays in the LLM's reasoning.
- Refunds also re-check the refundable amount inside the write transaction — defense in depth against state changes between precondition check and execution.
- Read tools return their facts as evidence items (normal IDs like `db:orders:ORD-1001`) so replanned steps can cite them.
- Unknown tools classify as high risk/approval required, but grounding rejects them before the approval gate anyway.
- Executed/failed/skipped audit rows are written by the executor (one place), `skipped` rows use actor `system`.
- Refund exactly $200 is auto-approved — policy says "$200 or less".

## Agent graph
- Node order is `understand → load_memory → retrieve` (the brief lists load_memory first) — customer IDs (including owners of referenced orders) are only known after entity extraction.
- The approval step is split into `approval_gate` (computes pending steps, writes `approval_requested`) and `await_approval` (only `interrupt()` + recording verdicts) — LangGraph re-runs the interrupted node on resume, so no audit write may precede `interrupt()` in that node.
- Preconditions run in `validate` (before approval) and again inside the executor right before each tool call; blocked steps become `skipped` immediately and never reach the approval gate.
- Replanning triggers on failed steps, precondition skips, or new read-tool evidence — not on operator rejections (re-proposing a rejected action would loop) and not on dependency skips.
- A step identical (tool + args) to one already executed this turn is skipped as "already executed earlier in this turn" — replans can't double-act (refund idempotency is a second guard).
- Grounding failure: one re-prompt with the error list (does not consume an iteration); a second failure produces a step-less `escalate` (if retrieval found missing/conflicting data) or `clarify` decision.
- Out-of-domain refusal is decided in code before any planner call (no entities + similarity < threshold) — saves free-tier tokens and can't be talked around.
- State holds only JSON-friendly dicts; `trace` events carry `turn_id` so the service returns just the current turn's trace from the multi-turn checkpoint.
- `get_llm()` returns one client exposing `complete(node, ..., tier=large|small)` — rather than `get_llm(tier)` — so FakeLLM can script by node name and usage is tracked in one place.
- Token/request usage is folded into graph state per turn (`usage`) and per session (`session_usage`) — survives Streamlit reruns and approval pauses.
- The response node falls back to a deterministic template when the model returns nothing or fails; citations are always humanised in code (`[policy:return_policy_v2§2]` → `[Return Policy v2 §2]`).
- Recursion limit raised to 60 — 3 plan iterations plus a grounding retry exceed LangGraph's default 25 supersteps.
- Live smoke test (`scripts/smoke_live.py`, `make smoke`) could not run: no GROQ_API_KEY — reported as SKIPPED.

## Memory
- Extraction returns an optional `refund_method` per memory; when present, code also writes `customers.preferred_refund_method` — the learned preference then drives `issue_refund`'s default deterministically, and shows in customer evidence.
- Extracted memories are only kept for customers that exist and were involved/mentioned, and for the current operator — the small model can't invent subjects.
- Loading returns ≤10 memories, preferences/facts before interaction summaries, newest first within each — per-turn summaries would otherwise crowd out durable preferences.
- If the extraction model fails, a deterministic interaction summary (intent → decision; executed tools) is stored instead.

## Interfaces
- Demo mode (`OPSPILOT_FAKE_LLM=1`, or the sidebar "LLM mode → demo" toggle) uses `DemoLLM`, which reads the real planning prompt and derives plans from the EVIDENCE block — citations are real IDs, and risk/approval/preconditions/grounding all run for real. Only the model's text is scripted.
- The response node uses the deterministic template in demo mode (DemoLLM returns no text for `respond`).
- Chroma client/embedder creation is guarded by a lock — found in the UI: the first retrieval creates the client from two threads at once and Chroma's Rust bindings fail.
- Streamlit keeps rich per-turn outcomes in `st.session_state`; the authoritative conversation lives in the LangGraph checkpoint (a page reload loses only the expanders, not the session).
- Chat input is disabled while approvals are pending; the service also raises `PendingApprovalError` if a new turn is started on a paused session.
- Dollar signs are escaped in UI markdown — Streamlit renders `$…$` as LaTeX.
- Policy labels show a version only for versioned docs (`*_vN`): "Return Policy v2 §2" but "Refund Policy §3".
- UI is tested headlessly with `streamlit.testing.v1.AppTest` (quick-start, approve, reject, chat refusal, memory delete) — screenshots aren't needed to verify behaviour.
- Quick-start buttons read `evals/scenarios.yaml` — one source for demo prompts and eval cases.

## Evaluation
- `evals/run_evals.py --demo` runs the same harness with DemoLLM and writes `evals/results_demo.md` — validates scenarios/scoring without a key; `evals/results.md` is reserved for the live run.
- Scoring: decision type (lists allowed where several are defensible, e.g. escalate vs act-with-escalation), expected tools ⊆ executed, approval requested == expected, forbidden tools never *executed* (a proposed-then-blocked refund is a pass: that's code enforcing policy), plus per-scenario checks.
- Approval cases auto-apply the scripted `approval_response`; unscripted pauses are auto-rejected so every case finishes.
- Default 20 s throttle between live cases; a crashing case is scored as failed instead of aborting the run.
- Tools are rendered to the planner as compact one-line signatures derived from each args JSON schema (name, type, enum values, regex pattern, description) instead of raw JSON schemas — planning prompt fell from ~4.2K to ~3.0K tokens, which matters under Groq's free-tier 12K TPM / 100K TPD on the 70B model.
- Runtime dir is resolved to an absolute path — a drive-less path (`/tmp/...` from Git Bash) made Chroma's Rust layer and Python disagree about where files live.
- Prompt iteration against the live model (target ≥ 11/12) is pending a GROQ_API_KEY.

## Polish
- README reports the demo-mode eval (12/12) and states plainly that the live `evals/results.md` needs a GROQ_API_KEY; no live score is claimed.

## Live validation (Groq key available)
- Groq no longer serves `llama-3.3-70b-versatile` / `llama-3.1-8b-instant` (check_models: MISSING). Switched to `openai/gpt-oss-120b` (large) / `openai/gpt-oss-20b` (small) — the closest open-weight large/small pair; both support JSON mode. Free tier: 8K tokens/min, 1K requests/day each.
- gpt-oss models reason before answering: `reasoning_effort=medium` for the large planner (with `low` it skipped over policy nuances), `low` for the small model; `max_tokens` raised to 4096 so reasoning can't truncate the JSON.
- Gemini fallback model is now `gemini-flash-latest` (2.5/2.0 Flash are retired). The provided key works but its free tier allows only 20 requests/day — not usable for tuning, so tuning ran on Groq with frugal per-scenario reruns.
- `OPSPILOT_PROVIDER=auto|groq|gemini` pins a provider (the committed eval must come from Groq alone).
- Daily-quota 429s ("tokens per day", Gemini's "PerDay" quota ids) raise `DailyLimitError`, which is not retried and deliberately not an `LLMError`, so nodes can't swallow it into a fallback answer; the eval stops and writes `results_partial.md`.
- The key had been pasted into `.env.example` (tracked); moved to the gitignored `.env` and the example restored before any commit.
- Scenario 2 fix: the model inferred "uncomfortable → opened, not defective → deny". Our data never records item condition, so this was an unsupported inference. Fixed with (a) a prompt rule never to infer condition from the return reason and an explicit eligibility checklist, and (b) a clarifying sentence in Return Policy v2 §4 (the warehouse inspects condition; support approves in-window returns unless the customer says the item was opened/used). The brief-required rule "opened electronics accepted only if defective" is unchanged.
- **Return Policy v2 §4 edit (knowledge-base change made during tuning, disclosed here).** The brief requires §4 to say that opened electronics are accepted only if defective. But NovaMart's data never records item condition: there is no opened/used/defective field on orders or items, and the request "the earbuds are uncomfortable" doesn't say either. With only that rule, gpt-oss-120b consistently inferred "uncomfortable ⇒ opened and not defective ⇒ deny", a conclusion drawn from no evidence. The policy text I wrote was ambiguous about who decides condition and when, so I added to §4: *"Item condition is inspected by the warehouse when the return arrives; support operators do not decide condition. At the support stage, a return request inside the category window is approved and refunded unless the customer states that a non-defective electronic item has been opened or used."* The required rule is unchanged, and no code-level check was changed (preconditions never enforced condition, since there is no data for it). The edit was needed because a prompt rule alone ("don't infer condition") did not stop the inference. With the policy clarified, the model grounds its approval in the evidence. Trade-off: this is a data edit made to fix a live failure, so the held-out set (`evals/holdout.yaml`) exists to check the fixes generalise.
- Scenario 3 fix: the model escalated because "refunds > $200 need manager approval" — it didn't know approvals are built in. Prompt now says: propose approval-gated actions; the system pauses them for a human.
- Scenario 3 fix: redundant `update_order_status → refunded` after a refund. Tool descriptions now say refunds/cancellations set the status; a no-op transition reports "no change needed" and doesn't trigger a replan (the step still doesn't run).
- Scenario 4 fix (retrieval): when a superseded chunk is retrieved, the current doc is searched with that chunk's own text, so v1 "30-day window" always brings v2 "windows by category". Before, the model correctly escalated for a missing rule.
- Response citations sometimes use fullwidth 【…】 brackets; `normalize_brackets` maps them to [ ] before grounding checks and humanising.
- Scenario 7 fix: the model answered the stock question (correctly using the fresher CSV) but didn't escalate the blocked exchange. Prompt now requires `escalate_to_human` when an order action is blocked by a MISSING INFORMATION gap, even if part of the question can be answered (Escalation SOP §2).

## Polish (during live validation)
- Policy numbers live in one place: each policy's front matter has a `rules:` block (refund limit, return windows, goodwill limits, max coupon, coupon validity, shipping promise/delay/coupon). `config.policy_rules()` merges the rules of effective, non-superseded docs (same version logic as retrieval), errors on a rule defined twice or missing, and reloads when a file changes. A test keeps the numbers consistent with the prose.
- Unknown product categories are treated as non-returnable by preconditions — the safe default.
- Every turn's full `TurnOutcome` (reasoning, plan, approvals, execution, sources, trace) is persisted in a `ui_turns` table by `AgentService` on start and on resume. The UI renders history from SQLite and reopens the most recent session on page load, so CLI sessions also appear in the UI. "Reset demo data" clears it with the rest of the runtime data.
- Groq's free tier for gpt-oss-120b is 200K tokens/day; tuning plus a full run exceeds it. Tuning reruns were per-scenario to conserve quota.

## Design (Streamlit app and project site)
- Direction: a calm technical manual. Warm paper background `#F6F4EF`, near-black ink `#1C1B19`, 1px rules `#D9D3C6`, one accent (deep green `#2E5E4E`) for links and key states. Radius is 2px, there are no shadows, and nothing animates.
- Type: **Newsreader** (headings) + **Public Sans** (body) + **IBM Plex Mono** (IDs, amounts, tool names). Newsreader is a text serif designed for reading on screens: it makes headings feel editorial rather than product-marketing. Public Sans is a neutral, very legible sans with good figures for tables. The support domain is full of identifiers (`ORD-1001`, `issue_refund`, `§3`), and a mono face makes them scannable. All three are OFL, self-hosted as Latin-subset woff2 (7 files, about 110 KB total), with `font-display: swap` on the site.
- Risk and status labels use muted text/background pairs (green, amber, red, warm grey), each at least WCAG AA contrast.
- Brand mark (`brand/mark.svg`): an open ring (the agent loop) broken on the right by a short green bar (the gate where code checks each proposal). It is the only source for the favicons, the web manifest icons and the Streamlit page icon and avatar. The operator's avatar (`brand/operator.svg`) is a separate outlined mark.
- Streamlit: the theme is in `.streamlit/config.toml` (same palette; fonts registered through `theme.fontFaces` from `app/static`). Framework icons are replaced with text in CSS: expander chevrons become `+`/`-`, the sidebar toggle says "Hide/Show sidebar", and the chat send arrow says "Send". The help tooltip icon was dropped in favour of a caption, and the dataframe hover toolbar is hidden (CSV download has its own labelled button). The select-box chevron is kept as a form-control affordance. The Deploy button is removed (`toolbarMode = "minimal"`), and the animated running indicator is hidden.
- Loading state: `AgentService` streams LangGraph task-start events (`stream_mode="tasks"`) to an optional `on_progress` callback. The app shows a static list of steps marked "done" and "now" (e.g. "Retrieving policies, order data, inventory and FAQ") instead of a spinner.
- Citations inside plan rationales and analysis are rendered with the same readable labels as the final response.
- `$` is escaped in all markdown and custom HTML, because Streamlit renders `$…$` as LaTeX even inside inline HTML.

## Project site (site/)
- Astro 7, static output, zero client JavaScript: no component is hydrated, and every page ships only HTML, inlined CSS, self-hosted woff2 fonts and WebP images.
- `SITE_URL` is read once, from `site/.env` (default `http://localhost:4321`), into Astro's `site`. Canonicals, the sitemap, Open Graph URLs, JSON-LD, robots.txt and llms.txt all derive from it.
- URLs use `build.format: "file"` + `trailingSlash: "never"` (`/how-it-works`, no `.html`).
- The sitemap is `sitemap-index.xml` (+ `sitemap-0.xml`): `@astrojs/sitemap` only writes an index plus chunks and has no option for a single `sitemap.xml`. robots.txt points to the index.
- Nothing on the site restates a number by hand: evaluation tables are parsed from `evals/*.md`, scenario prompts from `evals/scenarios.yaml`, outcomes from `evals/outcomes_main*.json`, setup steps from the README's Quick start and Testing sections, thresholds from the policy front matter and `config.py`, and the test count from the README. The check script verifies the built pages against those files.
- Scenario outcomes and screenshots come from scripted demo mode (the live quota was exhausted); every one is labelled as such, and a live run replaces the outcomes on the next build.
- Diagrams are the Mermaid blocks in `docs/architecture.md`, rendered to SVG at build time in headless Chromium with `htmlLabels: false` and the classic look. The build strips Mermaid's drop shadows, gradients, animation CSS, large radii and hard-coded greys/black, and fails if any remain.
- Share images: one 1200x630 PNG per page, rendered at build time from an HTML template (mark, page kicker and headline, and a real app screenshot) in headless Chromium.
- JSON-LD: WebSite, Person and SoftwareApplication on every page (one `@graph`), BreadcrumbList on inner pages matching the visible breadcrumbs. No LocalBusiness. The SoftwareApplication has no `offers`: nothing is sold or distributed.
- Side notes are `<aside>` elements named after their visible label (`aria-label="Side note: ..."`), because several asides on one page need unique accessible names. Scrollable tables sit in named `<section>` wrappers with `tabindex="0"` so keyboard users can scroll them.
- The 404 page is `noindex` without a canonical. Lighthouse's SEO audit reports 63 there (`is-crawlable`), which is correct for an error page.
- Link checking accepts HTTP 999 from linkedin.com only: LinkedIn returns it to every automated client, including headless Chromium, even for valid public profiles.
- Lighthouse runs through Playwright's Chromium over a debugging port (`chrome-launcher` could not spawn Chrome on this Windows machine). Each page is requested once before auditing, so the preview server's cold start doesn't skew the first audit.
- `npm audit`: zero vulnerabilities in production dependencies. Five high-severity advisories are in `lodash-es` under Mermaid's parser (`chevrotain`). Mermaid is a dev-only dependency that runs at build time on our own diagram text, and none of it ships.

## Flaky test investigation (one full `make site-check` run reported 45 failed / 98 passed)
- **Isolation audit.** Every runtime path resolves through `config.get_settings()` and `OPSPILOT_RUNTIME_DIR`, which `tests/conftest.py` sets to a fresh temp dir before any import. Nothing in `make site-check` (build, preview, Playwright, Lighthouse) touches `data/runtime/`, and no make target runs in parallel. A new session-wide guard wraps `sqlite3.connect` and `chromadb.PersistentClient` and raises if a test opens anything under `data/runtime/`, and each test also asserts the runtime before and after. The guard never fired.
- **Cross-process reproduction: not the cause.** `make test` passed 147/147 while the Streamlit app ran 44 agent turns on `data/runtime/`, and again while `make seed` rebuilt it 22 times. Five random-order runs (pytest-randomly) also passed.
- **Real bug 1: order dependence.** Several tests read business data without resetting it (e.g. `test_retrieval` expects ORD-1007 unrefunded). Running a refund test first made `test_order_evidence_has_computed_fields` fail (`0.0 == 149.0`); only the alphabetical file order hid it. Fix: the SQLite reset is now autouse before every test (about 70 ms each). pytest-randomly is pinned, so `make test` runs in random order and would expose any new dependence.
- **Real bug 2, the most likely cause of the 45 failures: mid-session Chroma rebuild.** `test_seed_is_idempotent` called `seed_all()` on the shared session store, deleting and recreating the Chroma collections every later test uses. Chroma builds its vector index in the background after `add`, so a query that arrives first can fail ("Nothing found on disk", seen earlier in this project). Simulating "Chroma broken right after that test" in the default order gives 46 failed / 97 passed, the same footprint as the incident (45 / 98). Direct stress (80 re-ingest-then-query cycles) did not reproduce the race, consistent with a timing-dependent failure under load.
  - Fix in code: `vector_store.ingest_all()` now waits until each rebuilt collection returns its full count and answers a probe query (30 s timeout, clear error otherwise). This also protects the app's "Reset data" button.
  - Fix in tests: the idempotency test and the client-cache race test each run against their own temp store, so the shared store is never rebuilt mid-session.

## Held-out fixes (branch `holdout-fixes`, found in the discarded partial Run A; merged only after a full Run A)
None of these is scenario-specific, and none loosens a safety rule.
- **Escalation is never blocked by missing records.** `escalate_to_human` had the same existence preconditions as `create_ticket`, so a request about an unknown order (H5, ORD-9999) could not be escalated: the step was skipped twice and the agent looped. Escalation now has no existence checks, because handing unknown or missing data to a person is its purpose. It stays low-risk and reversible. `create_ticket` keeps its checks.
- **Model typography is normalised.** gpt-oss writes IDs with U+2011 non-breaking hyphens ("ORD‑9999") and uses narrow no-break spaces, so IDs in responses could not be found by exact text or copied reliably. `humanize()` (final responses and UI rationales) maps U+2010/U+2011 to "-" and U+00A0/U+202F to a space.
- **Retrieval asks for the goodwill rule when it applies.** The deterministic hint queries never requested Loyalty Program §3 for an item past its return window (H6), so it was only retrieved when semantic search happened to match, as it did in main scenario 4. A delivered order with any item outside its current-policy window now adds a goodwill hint query.

## Fix found by the live replan probe (2026-09-28, after main runs 1-3)
- **Replans read current records.** A live two-operator probe (evidence in `evals/evidence/live_probes.jsonl`) had two sessions pause on the same $1,299 refund; the second operator approved first, then the first. The executor's pre-execution re-check correctly skipped the stale refund, but the replan worked from the evidence gathered at the start of the turn ("status delivered, refundable $1,299"), so the agent re-proposed the same refund until the iteration cap and its explanation repeated the stale figure. Now `observe` re-reads the orders and customers that blocked or failed steps refer to and replaces their evidence (`RetrievalBundle.upsert`) before replanning; read-tool results also replace stale items instead of being dropped as duplicates. Generic (any blocked or failed step), no prompt change, no safety rule touched; `test_stale_approval_replans_on_current_records` fails without it. Because `src/` changed, main runs 1-3 were redone on the new code.
- **Read tools are available to the planner but not described as a way to gather evidence** (the prompt only lists them in the tool catalogue). In live runs the model resolves requests from the retrieved evidence or escalates; it did not choose a lookup in the probe that named only a customer. Kept as a documented limitation rather than a prompt change made after evaluation.
