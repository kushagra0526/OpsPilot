# OpsPilot held-out evaluation

## Attempt 1 (partial, stopped by quota after 6/8)

- Started: 2026-09-28T00:28:43 · code: `97e7d7b` (clean working tree)
- Mode: **live** · provider `groq` · large `openai/gpt-oss-120b` · small `openai/gpt-oss-20b`
- **Run 1: 4/6 scenarios fully passing** (incomplete: 6 of 8 ran)
- Tokens, run 1: 60,707 total (large model 54,922, small model 5,785)
- Stopped at H7: Groq reported 198,285 of 200,000 daily tokens used for the planning model and refused the next request. H7 and H8 did not run. The partial report file was discarded under the quota rule; these rows are transcribed from the run's console output and `evals/token_ledger.jsonl`.
- An earlier start of this same attempt, on 2026-09-27 at commit `681976b`, passed H1 before the daily limit stopped it at H2; that partial report was discarded too. Between the two starts only instrumentation changed (per-tier token counts, progress callbacks for the UI, a Chroma readiness wait); no prompt, policy, retrieval or agent-logic change.

| # | Scenario | Checks | Result | Latency (s) | LLM calls | Tokens | Failed checks |
|---|---|---|---|---|---|---|---|
| H1 | Refund of exactly $200 (auto-limit boundary) | 6/6 | ✅ pass | 13.9 | 4 | 7,544 | — |
| H2 | Cancel a shipped order | 5/5 | ✅ pass | 21.8 | 4 | 8,218 | — |
| H3 | Coupon rules question | 6/6 | ✅ pass | 45.6 | 5 | 8,600 | — |
| H4 | One request covering two orders | 6/6 | ✅ pass | 34.0 | 4 | 8,721 | — |
| H5 | Unknown order ID | 3/5 | ❌ fail | 113.3 | 7 | 19,927 | decision_type (got act, want ['clarify', 'answer', 'escalate']); response_text (response content) |
| H6 | Platinum out-of-window return | 2/5 | ❌ fail | 18.4 | 4 | 7,697 | decision_type (got clarify, want act); expected_tools (executed [], want ⊇ ['issue_coupon']); coupon_limit (coupons []) |

Failure causes (diagnosed from the run's recorded outcomes and a retrieval check; all three are code or data-flow bugs, not prompt tuning):

- **H5, escalation blocked.** The agent tried `escalate_to_human` for the unknown order ORD-9999, but the escalation step had the same precondition as ordinary tickets ("the order must exist"). It was skipped twice, then a lookup failed, and the turn ended as `act` with nothing done.
- **H5, unsearchable IDs.** The model's reply wrote IDs with U+2011 non-breaking hyphens ("ORD‑9999"), so the exact-text check for "ORD-9999" failed (and copy-paste of IDs would too).
- **H6, goodwill rule not retrieved.** Loyalty Program §3 (goodwill coupon limits by tier) was not in the evidence for this request; the retrieval hints never asked for it when an item is past its return window. Without the rule, the model asked for a "preferred coupon amount" instead of issuing the platinum goodwill coupon.

### Changes made between Attempt 1 and Attempt 2

- `src/opspilot/safety/preconditions.py`: `escalate_to_human` has no existence checks (escalation is how missing or unknown records reach a person); `create_ticket` keeps them.
- `src/opspilot/agent/citations.py`: `normalize_typography()` maps U+2010/U+2011 to "-" and U+00A0/U+202F to a space in final responses and UI rationales.
- `src/opspilot/knowledge/retriever.py`: a delivered order with any item outside its current-policy return window adds a retrieval hint for the goodwill rule.
- No prompt text changed, no scenario was edited, and no safety rule was loosened. Tests added for all three (`tests/test_preconditions.py`, `tests/test_grounding.py`, `tests/test_retrieval.py`).

## Attempt 2 (after fixes)

- Started: 2026-09-28T10:38:58 · code: `79247b3` (clean working tree)
- Mode: **live** · provider `groq` · large `openai/gpt-oss-120b` · small `openai/gpt-oss-20b`
- **Run 1: 8/8 scenarios fully passing**
- Tokens, run 1: 74,819 total (large model 66,479, small model 8,340)

| # | Scenario | Checks | Result | Latency (s) | LLM calls | Tokens | Failed checks |
|---|---|---|---|---|---|---|---|
| H1 | Refund of exactly $200 (auto-limit boundary) | 6/6 | ✅ pass | 34.5 | 5 | 12,361 | — |
| H2 | Cancel a shipped order | 5/5 | ✅ pass | 40.9 | 4 | 8,873 | — |
| H3 | Coupon rules question | 6/6 | ✅ pass | 44.1 | 5 | 8,808 | — |
| H4 | One request covering two orders | 6/6 | ✅ pass | 38.7 | 4 | 9,529 | — |
| H5 | Unknown order ID | 5/5 | ✅ pass | 44.4 | 5 | 10,012 | — |
| H6 | Platinum out-of-window return | 5/5 | ✅ pass | 28.3 | 4 | 7,995 | — |
| H7 | Mixed-category order, only part eligible | 6/6 | ✅ pass | 43.1 | 4 | 9,707 | — |
| H8 | Standard tier out-of-window (no goodwill allowed) | 4/4 | ✅ pass | 31.0 | 4 | 7,534 | — |

Checks per scenario: decision type, expected tools executed, approval required (or not), forbidden tools never executed, plus scenario-specific checks (citations, conflict detection, audit events, refund orders/method/amount, coupon limits, email ordering).
