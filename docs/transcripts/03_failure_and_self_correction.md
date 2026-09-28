# 03: Failure and self-correction: a stale approval

Two operators open the same request in two sessions. Both plans pause for approval. Operator B approves first and the refund runs. Then operator A approves the plan that is now stale. The executor re-checks every precondition right before running a step, so A's refund is blocked; the observe node sees the blocked step and sends the agent back to planning.

The same live scenario was recorded twice: before and after a fix to the replan step. In the first run the guardrail held (no second refund), but the replan still used the order record read at the start of the turn, so the model proposed the same refund again until the 3-iteration cap and its explanation repeated the stale amount. The fix re-reads the records behind a blocked or failed step before replanning (`_refresh_records` in `src/opspilot/agent/nodes.py`, test `test_stale_approval_replans_on_current_records`). The second run shows the corrected behaviour.

- Source: `evals/evidence/live_probes.jsonl` (both runs, recorded as they happened)
- Model: openai/gpt-oss-120b (planning, responses) and openai/gpt-oss-20b (intent, memory) on Groq

## Goal (both operators)

> Process a full refund for ORD-1001, the laptop has a dead pixel.

## Run 1: before the fix (2026-09-28T12:22:29)

Operator B's session `probe-873f25`: issue_refund executed, send_customer_email executed.

Operator A's session `probe-280cf1`, after A approves:

Planning trace:

1. `understand: action: ORD-1001`
1. `load_memory: 0 memories loaded`
1. `retrieve: 15 evidence items {'policy': 8, 'faq': 3, 'db': 3, 'csv': 1}, 0 conflicts, 0 missing, in 1.66s`
1. `reason_and_plan: iteration 1: act, 2 steps, confidence 0.99`
1. `validate: act: 2 steps proposed, 0 blocked by preconditions`
1. `approval_gate: 1 step(s) need human approval`
1. `await_approval: s1 approved by operator-a`
1. `execute: s1:issue_refund=skipped, s2:send_customer_email=skipped`
1. `observe: replanning (iteration 2): 1 blocked/failed, new info=False`
1. `reason_and_plan: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 30.0s`
1. `reason_and_plan: iteration 2: act, 2 steps, confidence 0.97`
1. `validate: act: 2 steps proposed, 2 blocked by preconditions`
1. `observe: replanning (iteration 3): 1 blocked/failed, new info=False`
1. `reason_and_plan: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 34.0s`
1. `reason_and_plan: iteration 3: act, 2 steps, confidence 0.99`
1. `validate: act: 2 steps proposed, 2 blocked by preconditions`
1. `observe: iteration cap 3 reached; responding with what is known`
1. `respond: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 14.0s`
1. `respond: 1125 chars`
1. `save_memory: 1 new memories`

Plans per iteration:

- iteration 1: `act`, steps issue_refund (after none), send_customer_email (after s1)
- iteration 2: `act`, steps issue_refund (after none), send_customer_email (after s1)
- iteration 3: `act`, steps issue_refund (after none), send_customer_email (after s1)

Tool results:

- iteration 1 **issue_refund**: `skipped`, reason: order status is refunded; only delivered orders can be refunded; requested $1,299.00 exceeds the refundable remaining $0.00 (order total minus prior refunds, Refund Policy §4)
- iteration 1 **send_customer_email**: `skipped`, reason: dependency s1 not executed
- iteration 2 **issue_refund**: `skipped`, reason: order status is refunded; only delivered orders can be refunded; requested $1,299.00 exceeds the refundable remaining $0.00 (order total minus prior refunds, Refund Policy §4)
- iteration 2 **send_customer_email**: `skipped`, reason: dependency s1 not executed
- iteration 3 **issue_refund**: `skipped`, reason: order status is refunded; only delivered orders can be refunded; requested $1,299.00 exceeds the refundable remaining $0.00 (order total minus prior refunds, Refund Policy §4)
- iteration 3 **send_customer_email**: `skipped`, reason: dependency s1 not executed

Audit log:

| Event | Tool | Actor |
|---|---|---|
| proposed | issue_refund | agent |
| proposed | send_customer_email | agent |
| approval_requested | issue_refund | agent |
| approved | issue_refund | operator-a |
| skipped | issue_refund | system |
| skipped | send_customer_email | system |
| proposed | issue_refund | agent |
| skipped | issue_refund | system |
| proposed | send_customer_email | agent |
| skipped | send_customer_email | system |
| proposed | issue_refund | agent |
| skipped | issue_refund | system |
| proposed | send_customer_email | agent |
| skipped | send_customer_email | system |

Final structured result:

```json
{
  "goal": "Process a full refund for ORD-1001, the laptop has a dead pixel.",
  "status": "completed",
  "iterations": 3,
  "final_decision": "act",
  "actions": [
    {
      "tool": "issue_refund",
      "status": "skipped"
    },
    {
      "tool": "send_customer_email",
      "status": "skipped"
    },
    {
      "tool": "issue_refund",
      "status": "skipped"
    },
    {
      "tool": "send_customer_email",
      "status": "skipped"
    },
    {
      "tool": "issue_refund",
      "status": "skipped"
    },
    {
      "tool": "send_customer_email",
      "status": "skipped"
    }
  ]
}
```

Final explanation to the operator:

**1. What was found**  
- **Order**: ORD-1001 – delivered on 2026-09-22, total $1,299.00, electronics (laptop) [Order ORD-1001].  
- **Return eligibility**: Within the 14-day electronics return window and the defect (dead pixel) qualifies under Return Policy v2 §4 [Return Policy v2 §4].  
- **Refundable amount**: $1,299.00 (no prior refunds) [Order ORD-1001].  

**2. What was done (or not done) and why**  
- **Issue refund** – *skipped*.  
  > “order status is refunded; only delivered orders can be refunded; requested $1,299.00 exceeds the refundable remaining $0.00 (order total minus prior refunds, Refund Policy §4)”  

- **Send customer email** – *skipped* (dependency on the refund step not executed).  
  > “dependency s1 not executed”  

**3. Why**  
- The system reported the order as already **refunded**, making it ineligible for another refund and indicating a refundable remaining balance of $0.00, which violates Refund Policy §4 [Refund Policy §4].  
- Because the refund step did not execute, the follow-up email could not be sent (per tool dependency).  

*No coupon codes or ticket IDs were generated.*

## Run 2: after the fix (records re-read before replanning) (2026-09-28T14:01:21)

Operator B's session `probe-f4dd49`: issue_refund executed, send_customer_email executed.

Operator A's session `probe-56c941`, after A approves:

Planning trace:

1. `understand: action: ORD-1001`
1. `load_memory: 0 memories loaded`
1. `retrieve: 15 evidence items {'policy': 8, 'faq': 3, 'db': 3, 'csv': 1}, 0 conflicts, 0 missing, in 1.58s`
1. `reason_and_plan: iteration 1: act, 2 steps, confidence 0.99`
1. `validate: act: 2 steps proposed, 0 blocked by preconditions`
1. `approval_gate: 1 step(s) need human approval`
1. `await_approval: s1 approved by operator-a`
1. `execute: s1:issue_refund=skipped, s2:send_customer_email=skipped`
1. `observe: replanning (iteration 2): 1 blocked/failed, new info=False, records refreshed=2`
1. `reason_and_plan: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 22.0s`
1. `reason_and_plan: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 2 after 6.0s`
1. `reason_and_plan: iteration 2: answer, 0 steps, confidence 0.99`
1. `validate: answer: 0 steps proposed, 0 blocked by preconditions`
1. `respond: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 12.0s`
1. `respond: 986 chars`
1. `save_memory: 1 new memories`

Plans per iteration:

- iteration 1: `act`, steps issue_refund (after none), send_customer_email (after s1)
- iteration 2: `answer`, steps none

Tool results:

- iteration 1 **issue_refund**: `skipped`, reason: order status is refunded; only delivered orders can be refunded; requested $1,299.00 exceeds the refundable remaining $0.00 (order total minus prior refunds, Refund Policy §4)
- iteration 1 **send_customer_email**: `skipped`, reason: dependency s1 not executed

Audit log:

| Event | Tool | Actor |
|---|---|---|
| proposed | issue_refund | agent |
| proposed | send_customer_email | agent |
| approval_requested | issue_refund | agent |
| approved | issue_refund | operator-a |
| skipped | issue_refund | system |
| skipped | send_customer_email | system |

Final structured result:

```json
{
  "goal": "Process a full refund for ORD-1001, the laptop has a dead pixel.",
  "status": "completed",
  "iterations": 2,
  "final_decision": "answer",
  "actions": [
    {
      "tool": "issue_refund",
      "status": "skipped"
    },
    {
      "tool": "send_customer_email",
      "status": "skipped"
    }
  ]
}
```

Final explanation to the operator:

**1. What was found**  
- Order **ORD-1001** is in **refunded** status. A full refund of **$1,299.00** was already issued on **2026-09-28**, leaving a **refundable remaining amount of $0.00**. [Order ORD-1001]  

**2. What was done (or not done) and why**  
- **Refund not issued** – the `issue_refund` step was **skipped** because:  
  > “order status is refunded; only delivered orders can be refunded; requested $1,299.00 exceeds the refundable remaining $0.00 (order total minus prior refunds, Refund Policy §4)”  

- **Customer email not sent** – the `send_customer_email` step was **skipped** because it depended on the refund step, which was not executed.  

**3. Why**  
- Refunds cannot exceed the order total minus any prior refunds; this order has already been fully refunded, so the refundable amount is $0.00. [Refund Policy §4]  
- The system enforces that only **delivered** orders can be refunded, and this order’s status is **refunded**, not delivered. [Order ORD-1001]
