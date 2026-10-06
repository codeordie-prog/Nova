# Qutis Clinical Intelligence  
## Executive briefing for MYDAWA leadership

**Document type:** Solution overview (non-technical)  
**Audience:** Chief Technology Officer and senior product/clinical stakeholders  
**Prepared by:** Qutis AI Research Team  
**Version:** 1.0 — October 2026  

---

## Executive summary

Qutis is a dermatology-oriented clinical intelligence service designed to operate within the MYDAWA care network. It helps patients understand skin concerns and cosmetic product choices through structured, channel-native conversations (for example WhatsApp), while preserving human oversight, explicit uncertainty, and auditable safety logic.

The system combines **vision-assisted skin review**, **deterministic ingredient safety evaluation**, and **governed conversational guidance**. It is built so that clinical tone, escalation paths, and confidence reporting stay aligned with what the user’s journey actually supports—never beyond it.

This document explains purpose, operating model, guardrails, personality governance, proactive reliability, the AI capability mix at a high level, and the rules engine knowledge base. It intentionally omits implementation specifics, internal prompts, and infrastructure diagrams.

---

## 1. Purpose and role in the MYDAWA ecosystem

### 1.1 What Qutis is

Qutis mediates between patients and MYDAWA’s specialist dermatology workflow. It:

- Supports users with clear, practical guidance on skin concerns and product suitability.
- Structures intake, observations, and recommendations so clinicians receive high-signal context when human review is needed.
- Reduces friction and uncertainty without substituting for licensed medical judgment.

Qutis is positioned as a **skincare audit and advisory service**, not a standalone diagnostic authority. Copy and flows reinforce that professional consultation remains available through the Qutis / MYDAWA pathway.

### 1.2 What problem it solves

Patients often arrive with product labels, photos, and incomplete context. Clinicians need consistent triage and documentation. Qutis:

- Normalizes intake (profile, allergies, goals, pregnancy-related context where relevant).
- Separates **cosmetic product review** from **clinical skin assessment**, with explicit modes for how personalized an answer may be.
- Routes escalation when risk, low confidence, or policy requires a human physician.

### 1.3 Delivery model

Users interact through familiar messaging channels. Inbound messages are authenticated, deduplicated, and processed asynchronously so replies remain reliable under load. Outbound responses are sent through the same trusted messaging provider, preserving delivery status and auditability.

---

## 2. How Qutis works (conceptual flow)

At a high level, every meaningful interaction follows a governed pipeline:

1. **Ingress and identity** — The user is recognized on channel; messages and media are accepted only through secured webhooks.
2. **Orchestration** — A central coordinator interprets intent (greeting, skin photo, product label or name, follow-up) and decides which capabilities may run.
3. **Intake and memory** — Structured profile and session state are retained so later steps (product review, escalation) use consistent context.
4. **Optional skin audit** — When the user provides a skin image, a dedicated vision capability produces structured observations (concern, signals, completeness of audit fields).
5. **Product audit (when requested)** — INCI or label text is extracted and evaluated against the ingredient knowledge base and the user’s personalization mode.
6. **Decision confidence and tone** — A published scoring framework blends evidence coverage, user context, and (when applicable) vision confidence, then selects how assertive user-facing language may be.
7. **Response and escalation** — The user receives channel-appropriate guidance; cases below confidence or above risk thresholds trigger human-in-the-loop workflows.

This design keeps **fast conversational UX** separate from ** slower, evidence-heavy product evaluation**, while linking them when the user opts into a full personalized path.

---

## 3. Safety guardrails and decision confidence

### 3.1 Layered safety

Qutis applies multiple independent layers:

| Layer | Role |
|--------|------|
| **Clinical boundaries** | No replacement for physicians; emergency redirection; dermatologist connection via Qutis, not ad-hoc self-referral. |
| **Rules engine** | Deterministic checks against indexed ingredient safety literature; findings are independent of model “opinion.” |
| **Personalization ceilings** | Even strong ingredient match cannot produce fully personalized tone unless the user’s journey earned that mode. |
| **Decision confidence** | A single scalar drives **guidance tone** (full, cautious, clarify)—not whether a safety rule fired. |
| **Human escalation** | Low confidence, elevated risk, or explicit consent paths invoke clinical review with compiled context. |

### 3.2 Published framework — product audits

For cosmetic product reviews, Qutis implements the research framework **“Decision Confidence for Product Audits”** (Qutis AI Research, September 2026):

**Public paper:** [https://qutis-health.africa/research/papers/decision-confidence-product-audits](https://qutis-health.africa/research/papers/decision-confidence-product-audits)

In plain language, the framework:

- Combines **ingredient evidence coverage** (how much of the label resolves to indexed safety data), **user and audit context**, and **vision confidence** when a fresh skin photo path was taken.
- Assigns a **personalization mode** (*personalized*, *profile-only*, *generic*) from the user journey—not from the score itself (avoiding circular logic).
- Applies **mode ceilings** so generic journeys cannot read as fully personalized (for example caps at 0.50 for generic and 0.65 for profile-only).
- Maps final **decision confidence** to **guidance tone**: high confidence allows clearer recommendations; moderate confidence mandates hedging and evidence gaps; low confidence shifts to clarify-and-confirm behavior.

Safety rule outcomes from the rules engine are **not** overridden by decision confidence: a flagged ingredient remains flagged; confidence only governs how confidently the **surrounding narrative** is written.

A companion paper on **vision confidence for skin audits** defines how model certainty on visual observations feeds the personalized path; product-audit paper takes that score as an input where appropriate.

---

## 4. Personality, voice, and clinical boundaries (“Soul”)

Qutis behavior is governed by a dedicated **Soul** specification—an internal constitution for identity, mission, and communication—not a one-off prompt.

### 4.1 Identity and mission

- Qutis presents as **dermatological clinical intelligence for MYDAWA**, built by Qutis AI Research.
- Mission: improve care quality, clinical velocity, and patient understanding while respecting human oversight.
- Philosophy balances rigorous collective intelligence with an explicit ethic of goodwill toward patient wellbeing.

### 4.2 Non-negotiable clinical boundaries

The Soul document encodes hard limits, including:

- Medical assistant role, not physician replacement.
- Plain statement of uncertainty when evidence is limited.
- Urgent emergency guidance when symptoms suggest immediate risk.
- Escalation to human clinical review when confidence is low or risk is non-trivial.
- Offers to connect users with a dermatologist **through the Qutis / MYDAWA network**, not unstructured external referral.

### 4.3 Communication principles

- Concise, calm, empathetic, and useful.
- Warm and occasionally playful when appropriate, always professional.
- Plain language unless the user requests technical depth.
- Focused follow-up questions when intake is incomplete.

### 4.4 Why this matters for MYDAWA

Soul is version-controlled and applied consistently across channels and model updates. It gives MYDAWA a **reviewable contract** for brand-safe, clinically bounded AI behavior without exposing proprietary prompt text in partner materials.

---

## 5. Proactiveness, awareness, and reliability (“Awareness”)

Long-running care requires more than reactive replies. Qutis maintains an **Awareness** layer that listens to **internal completion signals** and acts when user-visible outcomes are ready or when work fails.

### 5.1 Signal-driven proactiveness

Internal signals are emitted when, for example:

- A diagnostic or audit job **completes successfully** and confidence exceeds policy thresholds—allowing timely follow-up or result delivery without the user polling.
- A job **fails after retries**—triggering a user-visible explanation and guidance to retry with complete inputs, rather than silent failure.

This pattern decouples **heavy processing** from **conversation timing**, so the agent can notify users when asynchronous work finishes.

### 5.2 State, causality, and memory

- **Short-term conversational memory** is persisted so multi-step flows (intake → photo → product review) remain coherent across interruptions.
- **Causality rules** ensure proactive actions do not contradict prior commitments or system state (for example, not claiming results that were never produced).

### 5.3 Confidence-linked human loop

Awareness integrates with **confidence thresholds**: when automated analysis cannot meet required certainty, the system records the gap and initiates **human-in-the-loop** consultation with compiled observations—mirroring medical faculty practice of referral when outside one’s certainty band.

### 5.4 Operational resilience

Failed or poisoned queue entries can surface as user-facing recovery messages rather than dropped conversations—supporting trust on a messaging channel where users expect a reply.

---

## 6. AI capability mix (high-level, no implementation detail)

Qutis deliberately combines **specialized open-source vision** with **managed conversational models**, orchestrated behind a single product experience.

### 6.1 Vision — skin and label understanding

- A **dedicated open-source vision model** runs on **rented GPU infrastructure** (NVIDIA-class accelerators) for skin-image analysis and related visual tasks.
- This keeps sensitive visual inference on a controllable stack tuned for clinical-style observation output (structured fields, not free-form diagnosis claims).
- Vision outputs feed audit completeness, escalation logic, and—when on the personalized product path—decision confidence inputs described in the published paper.

### 6.2 Conversation — hybrid model chain

- The **conversational agent** uses a **chain of models** spanning **frontier closed-source APIs** and **open-source or cost-efficient alternatives**, with automatic fallback when a provider is rate-limited or unavailable.
- Tool use (scheduling audits, invoking product rules, checking case status) is centralized so model swaps do not fragment business logic.
- Report-style subflows (for example long-form product summaries) can use a **separate chain** optimized for quality and cost, still subject to Soul boundaries and rules-engine facts.

### 6.3 Design intent for MYDAWA

| Concern | Approach |
|---------|----------|
| **Quality** | Best-available models for dialogue; specialized vision for images. |
| **Resilience** | Multi-model fallback; async workers; proactive failure notification. |
| **Governance** | Soul + rules engine + decision confidence sit **above** raw model output. |
| **Partner confidentiality** | No requirement for MYDAWA to host model weights; Qutis operates the blend as a managed service. |

Specific model names, provider accounts, prompt templates, and network topology are intentionally out of scope for this briefing.

---

## 7. Rules engine and ingredient knowledge base

### 7.1 Purpose

Cosmetic product advice must be **evidence-anchored**. The rules engine evaluates product requests against a structured **Cosmetic Ingredient Review (CIR)**–aligned index and linked safety literature—not against model hallucination alone.

### 7.2 Scale of knowledge

The index maps on the order of **47,000 ingredient line entries** to normalized identities, functions, and **retrievable safety assessment reports** (including PDF-sourced toxicology and safety narratives where available). Coverage metrics (evidence coverage *E* in the decision-confidence paper) measure what fraction of a user’s label resolves to indexed, citable material.

### 7.3 How evaluation works (conceptually)

1. **Extract** INCI or label lines from user input (photo OCR or text).
2. **Resolve** each line to index entries (synonyms, normalization).
3. **Apply rules** — contraindications, profile-aware checks (allergies, pregnancy flags where configured), and literature-backed findings.
4. **Enrich** downstream reporting with citations and gaps (“ingredient not indexed”) explicitly surfaced.
5. **Combine** with personalization mode and decision confidence for user-facing tone.

Deterministic rule hits are logged and reproducible—supporting audit, pharmacovigilance-style review, and CTO due diligence.

### 7.4 Relationship to AI

Large language models **narrate and structure**; the rules engine **decides indexed safety facts**. Decision confidence governs **how strongly** the narrative speaks, not whether a rule applies.

---

## 8. Integration, security, and compliance posture (summary)

- **Channel security:** Webhook HMAC verification; secrets managed via cloud secret stores in production.
- **Data separation:** Staging and production environments are isolatable; partner-visible URLs can be branded via custom domains.
- **Billing and fairness:** Usage metering and credit holds can gate expensive operations (vision, long reports) per product policy.
- **Regulatory framing:** User-facing disclaimers state that Qutis is advisory and does not replace professional medical consultation—aligned with Qutis Health public research and site language.

Detailed DPIAs, data residency, and MYDAWA-specific contractual terms are out of scope for this document and belong in a legal / security annex.

---

## 9. Suggested discussion topics for MYDAWA CTO

1. **Integration surface** — Webhooks, CRM/clinical handoff, and identifier mapping (patient vs. channel identity).
2. **Escalation SLA** — Human review queues, notification to MYDAWA clinicians, and audit logs.
3. **Mode defaults** — Whether MYDAWA prefers skin-first product review or profile-only paths for certain cohorts.
4. **Branding and domains** — Public webhook hostnames and patient-facing naming.
5. **Observability** — Metrics partners may access (volume, escalation rate, decision-confidence distribution) without model-internals exposure.

---

## References

1. Qutis AI Research — *Decision Confidence for Product Audits* (September 2026).  
   [https://qutis-health.africa/research/papers/decision-confidence-product-audits](https://qutis-health.africa/research/papers/decision-confidence-product-audits)

2. Qutis AI Research — *A Confidence Estimation Framework for Skin & Product Audits* (companion; vision confidence input).

3. Qutis Health — public product and compliance statements: [https://qutis-health.africa](https://qutis-health.africa)

---

**Disclaimer.** QUTIS AI IS A SKINCARE AUDIT AND ADVISORY SERVICE. IT IS NOT A MEDICAL ASSESSMENT TOOL AND DOES NOT REPLACE PROFESSIONAL MEDICAL CONSULTATION.

*End of document.*
