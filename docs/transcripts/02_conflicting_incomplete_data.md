# 02: Conflicting and incomplete data

An exchange request for an order with no recorded delivery date, for a product whose stock differs between the database (5) and the warehouse feed (0). Retrieval surfaces both problems; the model resolves the stock conflict by the fresher source and escalates the exchange because eligibility cannot be determined.

- Source: live UI walkthrough, session `sess-a7ed36`, 2026-09-28T12:09:37 (stored turn and audit rows, snapshot in `evals/evidence/transcript_sessions.json`)
- Model: openai/gpt-oss-120b (planning, responses) and openai/gpt-oss-20b (intent, memory) on Groq

## Goal

> Customer wants to exchange ORD-1004's earbuds for a new pair. Is EL-HP-02 in stock?

## Planning trace (graph nodes in order)

1. `understand`: mixed: ORD-1004
1. `load_memory`: 5 memories loaded
1. `retrieve`: 24 evidence items {'policy': 11, 'faq': 4, 'db': 3, 'csv': 1, 'memory': 5}, 2 conflicts, 1 missing, in 2.22s
1. `reason_and_plan`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 30.0s
1. `reason_and_plan`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 2 after 2.0s
1. `reason_and_plan`: iteration 1: act, 2 steps, confidence 0.97
1. `validate`: act: 2 steps proposed, 0 blocked by preconditions
1. `approval_gate`: no approval needed
1. `execute`: s1:escalate_to_human=executed, s2:send_customer_email=executed
1. `observe`: all steps settled; responding
1. `respond`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 1 after 15.0s
1. `respond`: LLM rate-limited/unavailable (RateLimitError); retrying… attempt 2 after 3.0s
1. `respond`: 1672 chars
1. `save_memory`: 1 new memories

## Plan

### Iteration 1: decision `act`, confidence 0.97

Analysis (model output, citations are evidence IDs):

> The customer wants to exchange the earbuds from order ORD-1004 and asks about stock for EL-HP-02. The order record [db:orders:ORD-1004] shows status delivered but delivery_date is NOT RECORDED, so return eligibility cannot be determined. Per escalation policy, missing required data mandates escalation before any irreversible action [policy:escalation_sop§2][policy:escalation_sop§4]. Exchange is not a direct operation; NovaMart requires a return for refund then a new order [faq:faq-14]. Stock information for EL-HP-02 is conflicting: DB shows 5 units (2026-09-20) but the CSV warehouse feed (2026-09-26) shows 0, which is fresher and therefore authoritative [db:products:EL-HP-02][csv:inventory:EL-HP-02]. Thus the item is out of stock. The appropriate response is to inform the customer of the out‑of‑stock status and that we must escalate the exchange request due to the missing delivery date.

- `s1` **escalate_to_human** (runs after: none)
  - args: `{"summary": "Exchange request for order ORD-1004 cannot be processed due to missing delivery date; also EL-HP-02 is out of stock.", "priority": "high", "customer_id": "C-001", "order_id": "ORD-1004"}`
  - rationale: Missing delivery date prevents return eligibility; policy requires escalation before any action [policy:escalation_sop§2][policy:escalation_sop§4].
- `s2` **send_customer_email** (runs after: s1)
  - args: `{"customer_id": "C-001", "subject": "Exchange request and stock update", "body": "Dear Aarav,\n\nThank you for contacting NovaMart. We have checked the stock for model EL-HP-02 (Nova Buds Pro) and, according to our latest warehouse feed, it is currently out of stock.\n\nRegarding your request to exchange the earbuds from order ORD-1004, we are unable to process the exchange at this time because the delivery date for that order is missing from our records, which prevents us from confirming return eligibility. We have escalated this case to a specialist who will review the details and contact you within one business day.\n\nWe appreciate your patience.\n\nBest regards,\nNovaMart Support Team"}`
  - rationale: Inform the customer of stock status and that the case has been escalated; email is sent after escalation step.

## Conflicts and missing data found by retrieval

- policy_version: Return Policy v1 (return_policy_v1, effective 2025-01-01) is superseded by v2 (return_policy_v2, effective 2026-03-01). Applying return_policy_v2; return_policy_v1 chunks are kept for reference only and must not be applied.
- inventory: Stock for EL-HP-02 differs: DB says 5 (updated 2026-09-20T09:00), CSV feed says 0 (snapshot 2026-09-26T18:00). Using CSV feed (snapshot 2026-09-26T18:00) as it is fresher than DB (2026-09-20T09:00). Resolved stock: 0.
- missing: ORD-1004 has no delivery_date recorded (status delivered); return eligibility cannot be determined

## Risk and approval (code guardrails)

| Step | Risk | Needs approval | Reasons |
|---|---|---|---|
| s1 | low | no | base risk low; reversible |
| s2 | low | no | base risk low; not reversible |

No step needed approval: escalation and email are low risk.

## Tool calls

- `s1` **escalate_to_human**: `executed` (start +0 ms, took 14 ms)
  - input: `{"summary": "Exchange request for order ORD-1004 cannot be processed due to missing delivery date; also EL-HP-02 is out of stock.", "priority": "high", "customer_id": "C-001", "order_id": "ORD-1004"}`
  - output: Escalated to a human agent: high-priority ticket TKT-KSKJ
- `s2` **send_customer_email**: `executed` (start +30 ms, took 25 ms)
  - input: `{"customer_id": "C-001", "subject": "Exchange request and stock update", "body": "Dear Aarav,\n\nThank you for contacting NovaMart. We have checked the stock for model EL-HP-02 (Nova Buds Pro) and, according to our latest warehouse feed, it is currently out of stock.\n\nRegarding your request to exchange the earbuds from order ORD-1004, we are unable to process the exchange at this time because the delivery date for that order is missing from our records, which prevents us from confirming return eligibility. We have escalated this case to a specialist who will review the details and contact you within one business day.\n\nWe appreciate your patience.\n\nBest regards,\nNovaMart Support Team"}`
  - output: Emailed aarav.mehta@example.com: 'Exchange request and stock update'

## Audit log

| Time | Event | Tool | Risk | Actor |
|---|---|---|---|---|
| 12:09:15.741 | proposed | escalate_to_human | low | agent |
| 12:09:15.757 | proposed | send_customer_email | low | agent |
| 12:09:15.798 | executed | escalate_to_human | low | agent |
| 12:09:15.837 | executed | send_customer_email | low | agent |

## Final structured result

```json
{
  "goal": "Customer wants to exchange ORD-1004's earbuds for a new pair. Is EL-HP-02 in stock?",
  "status": "completed",
  "decision_type": "act",
  "iterations": 1,
  "actions": [
    {
      "tool": "escalate_to_human",
      "status": "executed",
      "result": "Escalated to a human agent: high-priority ticket TKT-KSKJ"
    },
    {
      "tool": "send_customer_email",
      "status": "executed",
      "result": "Emailed aarav.mehta@example.com: 'Exchange request and stock update'"
    }
  ],
  "conflicts": [
    "policy_version",
    "inventory"
  ],
  "missing": [
    "ORD-1004 has no delivery_date recorded (status delivered); return eligibility cannot be determined"
  ]
}
```

## Final explanation to the operator

**1. What was found**

- **Order ORD-1004** – status *delivered* but **delivery_date is NOT RECORDED**, so return eligibility cannot be determined. [Order ORD-1004]  
- **Stock for EL-HP-02** – CSV warehouse feed (2026-09-26) shows **0 units** (newer than DB record of 5). [Product record EL-HP-02] [Warehouse feed EL-HP-02]  
- **Exchange policy** – NovaMart requires a return for refund before a new purchase; direct exchanges are not automatic. [FAQ faq-14]  

**2. What was done (or not done) and why**

- **Escalated** the case to a human specialist – created high-priority ticket **TKT-KSKJ**.  
  - Reason: *“required data is missing from the order … no delivery date is recorded, so return eligibility cannot be determined”* [Escalation SOP §2] and *“no … irreversible action may be taken until a human agent has reviewed the case”* [Escalation SOP §4].  
- **Sent a customer email** to aarav.mehta@example.com informing them that the earbuds are out of stock and that the exchange request has been escalated. [step s2]  

**No exchange or refund was processed** because the missing delivery date blocks eligibility and the item is out of stock, per the escalation SOP and exchange FAQ.  

**3. Why**

- Missing delivery date prevents eligibility assessment → escalation required [Escalation SOP §2].  
- Contradictory/insufficient data bars any irreversible action until a human reviews [Escalation SOP §4].  
- Direct exchanges are not supported; a return-then-new-order flow is needed, but the product is currently unavailable [FAQ faq-14].  
- The freshest inventory source (CSV feed) shows zero stock, confirming out-of-stock status [Warehouse feed EL-HP-02].
