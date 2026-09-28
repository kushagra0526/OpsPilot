# OpsPilot write-up

## What I built

OpsPilot is a support-operations agent for a fictional store. It takes a goal in plain English, retrieves evidence from four internal sources, plans typed tool calls, asks a person before risky actions, executes, re-plans when blocked, and explains the result with citations. Stack: Groq free tier (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`), LangGraph, Chroma, SQLite, Streamlit.

## Design decisions

- **The model decides; code sets the limits.** Every plan, including refusals, comes from the LLM. Code does not choose actions, but it constrains them like permissions: a risk policy decides what needs approval, preconditions (refund at most the refundable amount, return windows, valid status changes) are checked before approval and again right before each tool runs, and every plan must cite retrieved evidence. A prompt asking for a $5,000 refund on a $149 order cannot succeed even if the model complies.
- **Evidence as IDs.** Every retrieved item has an ID (`policy:return_policy_v2§2`, `db:orders:ORD-1001`). Plans must cite them, superseded policy versions cannot justify an action, and the explanation renders them as readable references.
- **A graph with checkpoints.** LangGraph plus SQLite checkpoints gives a real pause for approval that survives a page reload, and an observe step that sends the agent back to planning when a step is blocked or fails.
- **Data problems on purpose.** The seed data contains an order without a delivery date and a stock count that differs between the database and the warehouse feed, so their handling is visible.

## Limitations

- The formal three-run evaluation (36/36) ran on code from before my last two changes: re-planning on re-read records, and refusals moving from a code rule to the model. It needs a rerun; the free quota did not allow one before the deadline. The held-out set passed 8/8 on its second attempt, and a live walkthrough of all 12 goals in the UI found no safety violations.
- The planner has read tools but is not told to use them to gather evidence; in live runs it works from the retrieved evidence or escalates.
- Approval precision and recall are both 1.00 over 44 live scenario runs, but only 6 runs needed approval.
- Single operator, no authentication, SQLite, mock email outbox.

## What I would do differently

- **Run the evaluation continuously.** I found the stale-record bug only by probing a two-operator race. I would add concurrency and failure cases to the scored set from the start and budget quota for reruns.
- **Fault injection as a feature.** Malformed model output and blocked steps are handled and tested with doubles; I would add a switch to inject tool timeouts and invalid responses in a live run.
- **Teach the planner to look things up.** Describe the read tools in the prompt as a way to gather missing facts, then measure the effect on escalations.
- **A per-run report file.** Each turn already produces a structured outcome; I would also write it to a JSON and Markdown file per run.
