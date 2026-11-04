# Qutis safety guardrails for skin and product reviews

**Audience:** Anthropic (non-technical overview)  
**Prepared by:** Qutis AI Research Team  
**Date:** November 2026

---

## 1. Summary (one minute)

Qutis helps people on messaging channels (for example WhatsApp) with **skin appearance reviews** and **cosmetic product checks**. We **orchestrate multiple agents**—including intake, vision, and reporting—alongside a **deterministic rules engine** backed by a curated ingredient knowledge base.

Our guardrails rest on three ideas:

- Measure **how sure we are** before sounding definitive.
- **Never let confident language override hard safety findings** from the rules engine.
- **Escalate to human clinicians** when confidence or risk requires it.

---

## 2. Rules engine and ingredient knowledge base

When a user submits a product label or ingredient list, Qutis does **not** rely on the chat model alone to judge safety.

A **rules engine** looks up each ingredient in a PostgreSQL-backed index of roughly **47,000 ingredient entries**. Each entry is mapped to a normalized identity and **retrievable safety assessment reports** (including literature such as CIR toxicology summaries where available).

**What the engine does:** Applies deterministic safety logic per ingredient and profile context (allergies, pregnancy-related flags where collected).

**What it does not do:** Guess from the open internet at review time. Coverage is measured by how much of the label resolves to indexed evidence.

**Why it matters:** Model text can sound persuasive when evidence is thin. Rule findings fire **independently** of how confident the report is allowed to sound.

Technical detail on evidence coverage and product scoring: see **Paper 2** in section 5.

---

## 3. Paper 1 — Skin reviews: confidence and human oversight

**Title:** A Confidence Estimation Framework for Skin and Product Audits  
**Published:** June 2026  
**Link:** https://qutis-health.africa/research/papers/confidence-estimation-framework

**Plain-language idea:** A vision model trained on a **custom dermatological dataset** describes what it sees in a skin photo. Qutis then turns the model's internal certainty into a **confidence score** per observation and combines scores across observations.

**Guardrail:** If confidence falls below a set threshold, the case is **not** shipped as a final automated report. It is routed to **qualified human review**, with referral to a licensed professional when appropriate. Low certainty triggers oversight—not overconfident copy.

---

## 4. Paper 2 — Product reviews: decision confidence and tone

**Title:** Decision Confidence for Product Audits  
**Published:** September 2026  
**Link:** https://qutis-health.africa/research/papers/decision-confidence-product-audits

**Plain-language idea:** Product advice must reflect **what the user actually provided**—label evidence, profile context, and (when applicable) a recent skin photo. Qutis computes **decision confidence** from: (a) how much of the ingredient list is backed by the indexed knowledge base, (b) how complete the user's context is, and (c) vision confidence when a skin audit was part of the journey.

**Personalization modes** cap how personalized the answer may sound (no skin photo means the system must not speak as if it saw the user's skin). **Guidance tone** (full, cautious, or clarify) follows decision confidence—but **safety rules still apply regardless of tone**.

**Guardrail:** Even a perfect ingredient match cannot produce overconfident, fully personalized language unless the user's journey earned that mode. The skin confidence score from Paper 1 is an input when both skin and product are in scope.

---

## 5. How the pieces fit together

1. **User intake** — Profile, allergies, goals; optional skin photo and product label or name.
2. **Skin path (optional)** — Vision observations, confidence scoring, gate or human review (Paper 1).
3. **Product path** — Ingredient extraction, **rules engine** against ~47k indexed ingredients, structured findings.
4. **Decision confidence** — Blends evidence, context, and vision when used; sets how assertive product guidance may be (Paper 2).
5. **Delivery** — Channel reply; dermatology escalation via Qutis when policy and consent require it.

**Key separation:** Confidence and tone govern **wording**; the rules engine governs **ingredient safety outcomes**.

---

## 6. Full papers (technical detail)

**Skin — vision confidence, aggregation, escalation gate**

https://qutis-health.africa/research/papers/confidence-estimation-framework

**Product — evidence coverage, personalization modes, decision confidence, guidance tone**

https://qutis-health.africa/research/papers/decision-confidence-product-audits

**Qutis Health (website)**

https://qutis-health.africa
