# 01: Multi-step planning with human approval

A $1,299 refund request. The model checks eligibility against the current policy, plans the refund and a dependent confirmation email, the risk guardrail pauses the refund for a person, and after approval both steps run in dependency order.

- Source: live UI walkthrough, session `sess-dda171`, 2026-09-28T12:05:35 (stored turn and audit rows, snapshot in `evals/evidence/transcript_sessions.json`)
- Model: openai/gpt-oss-120b (planning, responses) and openai/gpt-oss-20b (intent, memory) on Groq

## Goal

> Process a full refund for ORD-1001, the laptop has a dead pixel.

## Planning trace (graph nodes in order)

1. `understand`: action: ORD-1001
1. `load_memory`: 2 memories loaded
1. `retrieve`: 17 evidence items {'policy': 8, 'faq': 3, 'db': 3, 'csv': 1, 'memory': 2}, 0 conflicts, 0 missing, in 1.62s
1. `reason_and_plan`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 24.0s
1. `reason_and_plan`: iteration 1: act, 2 steps, confidence 0.99
1. `validate`: act: 2 steps proposed, 0 blocked by preconditions
1. `approval_gate`: 1 step(s) need human approval
1. `await_approval`: s1 approved by operator-1
1. `execute`: s1:issue_refund=executed, s2:send_customer_email=executed
1. `observe`: all steps settled; responding
1. `respond`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 3.0s
1. `respond`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 2 after 6.0s
1. `respond`: 1445 chars
1. `save_memory`: 1 new memories

## Plan

### Iteration 1: decision `act`, confidence 0.99

Analysis (model output, citations are evidence IDs):

> The order ORD-1001 is delivered (delivery_date 2026-09-22) and today is 2026-09-27, so it is within the 14‑day return window for electronics [policy:return_policy_v2§2]. The laptop is reported to have a dead pixel, which qualifies as a defect, and opened electronics are accepted when defective [policy:return_policy_v2§4]. The order total is $1299.00 with no prior refunds, so the refundable remaining amount is $1299.00 [db:orders:ORD-1001]. This amount exceeds $200, therefore a manager approval is required, but the system will pause the action for approval automatically [policy:refund_policy§3]. The refund does not exceed the order total, satisfying the limit rule [policy:refund_policy§4]. The customer's preferred refund method is not recorded, so we will use the original payment method as default [policy:refund_policy§2].

- `s1` **issue_refund** (runs after: none)
  - args: `{"order_id": "ORD-1001", "amount": 1299.0, "reason": "Dead pixel on laptop"}`
  - rationale: Refund the full order amount for a defective electronic item within the return window; amount is within order total and requires manager approval which the system will enforce automatically [policy:return_policy_v2§2][policy:return_policy_v2§4][policy:refund_policy§3][policy:refund_policy§4][db:orders:ORD-1001]
- `s2` **send_customer_email** (runs after: s1)
  - args: `{"customer_id": "C-001", "subject": "Your refund for order ORD-1001", "body": "Dear Aarav Mehta,\n\nWe have processed a refund of $1299.00 for your order ORD-1001 due to the reported dead pixel on the Nova 14 Laptop. The refund will be returned to your original payment method (card) and should appear within 5–7 business days.\n\nIf you have any further questions, please let us know.\n\nBest regards,\nNovaMart Support"}`
  - rationale: Policy requires a confirmation email after every refund, stating amount, method, and timing [policy:refund_policy§6][policy:refund_policy§2]

## Risk and approval (code guardrails)

| Step | Risk | Needs approval | Reasons |
|---|---|---|---|
| s1 | high | yes | $1,299.00 exceeds the $200 auto-refund limit (Refund Policy §3); refunds are irreversible |
| s2 | low | no | base risk low; not reversible |

The refund paused at the approval gate. Operator `operator-1` approved it in the UI with the note "Photo of the dead pixel checked"; the email (low risk) waited for the refund.

## Tool calls

- `s1` **issue_refund**: `executed` (start +0 ms, took 52 ms)
  - input: `{"order_id": "ORD-1001", "amount": 1299.0, "reason": "Dead pixel on laptop"}`
  - output: Refunded $1299.00 for ORD-1001 to the original payment method; order is now refunded
- `s2` **send_customer_email**: `executed` (start +67 ms, took 23 ms)
  - input: `{"customer_id": "C-001", "subject": "Your refund for order ORD-1001", "body": "Dear Aarav Mehta,\n\nWe have processed a refund of $1299.00 for your order ORD-1001 due to the reported dead pixel on the Nova 14 Laptop. The refund will be returned to your original payment method (card) and should appear within 5–7 business days.\n\nIf you have any further questions, please let us know.\n\nBest regards,\nNovaMart Support"}`
  - output: Emailed aarav.mehta@example.com: 'Your refund for order ORD-1001'

## Audit log

| Time | Event | Tool | Risk | Actor |
|---|---|---|---|---|
| 12:05:35.251 | proposed | issue_refund | high | agent |
| 12:05:35.288 | proposed | send_customer_email | low | agent |
| 12:05:35.311 | approval_requested | issue_refund | high | agent |
| 12:05:39.311 | approved | issue_refund | high | operator-1 |
| 12:05:39.382 | executed | issue_refund | high | agent |
| 12:05:39.420 | executed | send_customer_email | low | agent |

## Final structured result

```json
{
  "goal": "Process a full refund for ORD-1001, the laptop has a dead pixel.",
  "status": "completed",
  "decision_type": "act",
  "iterations": 1,
  "actions": [
    {
      "tool": "issue_refund",
      "status": "executed",
      "result": "Refunded $1299.00 for ORD-1001 to the original payment method; order is now refunded"
    },
    {
      "tool": "send_customer_email",
      "status": "executed",
      "result": "Emailed aarav.mehta@example.com: 'Your refund for order ORD-1001'"
    }
  ],
  "conflicts": [],
  "missing": []
}
```

## Final explanation to the operator

## 1. What was found  
- **Order details:** ORD-1001 was delivered on 2026-09-22 (5 days ago) and the total paid was **$1299.00** with no prior refunds — refundable amount $1299.00[Order ORD-1001].  
- **Return eligibility:** The laptop is an electronic product, so the 14-day return window applies and the request is within it[Return Policy v2 §2].  
- **Defect qualification:** The reported dead pixel qualifies as a defect, which permits the return of an opened electronic item[Return Policy v2 §4].  
- **Refund size:** The requested refund exceeds $200, which normally requires manager approval[Refund Policy §3].  

## 2. What was done  
- **Refund issued:** **$1299.00** was refunded to the original payment method (card). *Executed* (step s1).  
- **Customer notified:** An email confirming the refund was sent to **aarav.mehta@example.com**. *Executed* (step s2).  

No denial was required; the action proceeded after the system handled the necessary approval step.

## 3. Why  
- The return window and defect status satisfy the return policy for electronics[Return Policy v2 §2][Return Policy v2 §4].  
- The refund amount does not exceed the order total, complying with the limit rule[Refund Policy §4].  
- Refunds are required to be paid to the original payment method unless a preference is recorded, which matches the action taken[Refund Policy §2].  
- Sending a confirmation email is mandated for every refund[Refund Policy §6].
