# OpsPilot write-up

## What I built

OpsPilot is a support-operations agent for a fictional store, NovaMart. An operator gives it a goal in plain English; it retrieves evidence from four internal sources, plans typed tool calls, asks a person before risky actions, executes, re-plans when something is blocked, and explains the result with citations. It runs on Groq's free tier (`openai/gpt-oss-120b` for planning, `openai/gpt-oss-20b` for intent and memory), LangGraph, Chroma, SQLite and Streamlit.

## Design decisions

- **The model decides; code sets the limits.** Every plan, including refusals, comes from the LLM. Code does not choose actions, but it constrains them like permissions: a risk policy decides what needs approval, preconditions (refund at most the refundable amount, return windows, valid status changes) are checked before approval and again right before each tool runs, and every plan must cite retrieved evidence. A prompt injection asking for a $5,000 refund on a $149 order cannot succeed even if the model goes along with it.
- **Evidence as IDs.** Every retrieved item has an ID (`policy:return_policy_v2§2`, `db:orders:ORD-1001`). Plans must cite them, superseded policy versions cannot justify an action, and the explanation renders them as readable references. This made grounding checkable instead of a hope.
- **A graph with checkpoints.** LangGraph plus SQLite checkpoints gives a real pause for approval that survives a page reload, and an observe step that sends the agent back to planning when a step is blocked or fails (at most three iterations).
- **Data problems on purpose.** The seed data contains an order without a delivery date and a stock count that differs between the database and the warehouse feed, so the handling of incomplete and conflicting data is visible, not theoretical.

## Limitations

- The formal three-run evaluation (36/36) ran on code from before my last two changes: re-planning on re-read records, and refusals moving from a code rule to the model. It needs a rerun, which the free token quota did not allow before the deadline. The held-out set passed 8/8 on its second attempt, and a live walkthrough of all 12 goals in the UI found no safety violations.
- The planner has read tools but is not told to use them to gather evidence; in live runs it works from the retrieved evidence or escalates.
- Approval precision and recall are both 1.00 over 44 live scenario runs, but only 6 of those runs needed approval, so this says little.
- Single operator, no authentication, SQLite, mock email outbox.

## What I would do differently

- **Run the evaluation continuously.** I found the stale-record bug only by probing a two-operator race by hand. I would add concurrency and failure cases to the scored set from the start, and budget quota for reruns after every fix.
- **Fault injection as a feature.** Tool timeouts, invalid tool responses and malformed model output are handled, but they are tested with doubles, not injectable in a live run. I would add a switch to inject them and show recovery in the UI.
- **Teach the planner to look things up.** Describe the read tools in the prompt as a way to gather missing facts, then measure whether multi-step lookups improve the escalation rate.
- **A per-run report file.** Each turn already produces a structured outcome; I would write it to a JSON and Markdown file per run so a reviewer can audit a session without the UI.
