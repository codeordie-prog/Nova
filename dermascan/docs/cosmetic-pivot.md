# Cosmetic Intelligence Pivot

Step-by-step rollout of cosmetic-intelligence flows. Each phase ships behind feature
flags with legacy behavior as the default.

## Target user flow

1. Greet and explain cosmetic decision support (not medical diagnosis).
2. Skin snapshot first → analyze → persist profile → detect risk for optional escalation.
3. Product check (image or name) → rules engine against latest profile/context.
4. Decision confidence → tailored feedback → consent-gated escalation when needed.

## Feature flags

All default to **off** (legacy behavior).

| Variable | Purpose |
|----------|---------|
| `COSMETIC_PIVOT_ENABLED` | Master switch for pivot routing/intake changes |
| `COSMETIC_SKIN_FIRST_ROUTING` | Require fresh skin snapshot before product review |
| `COSMETIC_TEXT_PRODUCT_REVIEW` | Product review from name/text without MedGemma |
| `COSMETIC_GUIDANCE_MODE` | Apply decision-confidence `guidance_mode` to product reports |
| `COSMETIC_CONSENT_ESCALATION` | Risk + consent escalation instead of confidence-only |

Helpers: `dermascan.shared.cosmetic_feature_flags`.

## Personalization modes

Product rules engine accepts `personalization_mode` on `RulesEngineRequest`:

| Mode | Meaning |
|------|---------|
| `personalized` | Vision-derived skin audit + profile (default) |
| `profile_only` | Text profile only (goals, allergies, pregnancy) |
| `generic` | Ingredient safety only; user skipped skin snapshot |

Derivation helper: `dermascan.shared.cosmetic_profile.derive_personalization_mode`.

## Implementation steps (review order)

| Step | Status | Description |
|------|--------|-------------|
| 0.1 | done | Feature flags + this doc |
| 1.1 | done | `PersonalizationMode` on `RulesEngineRequest` |
| 1.2 | done | Intake session fields (`skin_snapshot_skipped`, `personalization_mode`) |
| 1.3 | done | `derive_personalization_mode` + unit tests |
| 2.1 | done | Wire mode into product signal flow |
| 2.2 | done | Mode-aware report disclaimers |
| 2.3 | done | Decision confidence (log-only) |
| 3.1 | done | MedGemma additive cosmetic schema |
| 3.2 | done | Rules bridge adapter |
| 3.3 | done | Persist skin snapshot |
| 4.1 | done | Orchestrator greeting + positioning |
| 4.2 | done | Opt-out capture (`skin_snapshot_skipped`) |
| 4.3 | done | Relax product routing when skipped |
| 5.1 | done | Text-only product path |
| 6.1 | done | Skin-first routing (fresh snapshot before product review) |
| 6.1b | done | Skin prerequisite enqueue + auto-chain product review |
| 6.2 | pending | Deprecate combined intent (optional) |
| 7.1 | done | Guidance mode → product report prompts + footers |
| 8.1 | done | Risk-tier + consent-gated escalation |
| 8.2a | done | Consent offer keyed by `client_id` (cross-turn acceptance) |
| 8.2b | done | Enrich audit context + orchestrator meds intake |
| 8.2c | done | MedGemma risk rubric + deterministic watch bump |
| 8.2d | done | User-initiated escalation builder |
| 9.x | pending | Legacy cleanup |

## Smoke tests (run before enabling flags in staging)

- A: New user, skin photo → skin report
- B: User with skin audit, product image → product report
- C: User skips skin, product name → generic disclaimer report
- D: Low confidence product → cautious copy, no auto-escalation
- E: Skin watch risk → offer only; accept → escalation job
