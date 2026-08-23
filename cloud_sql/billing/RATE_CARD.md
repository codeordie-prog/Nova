# Rate card v2026-08-21

Internal metering credits for orchestrator LLM calls.

## Credit scale

**100 credits = $1.00 USD** of provider API list price (`BILLING_CREDITS_PER_USD=100`).

Retail pricing (KES per credit sold to users) is separate — set at Paystack top-up with markup over this cost base.

## Orchestrator model chain (default)

From `model_fallback_chain._DEFAULT_ORCHESTRATOR_MODEL_CHAIN`:

| Model key | USD input / 1M | USD output / 1M | Credits in / 1M | Credits out / 1M | Source |
|-----------|----------------|-----------------|-----------------|------------------|--------|
| `google:gemini-3.5-flash` | $1.50 | $9.00 | **150** | **900** | [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| `google:gemini-3.1-flash-lite` | $0.25 | $1.50 | **25** | **150** | [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| `openai:gpt-4o` | $2.50 | $10.00 | **250** | **1000** | [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |
| `openai:gpt-5` | $1.25 | $10.00 | **125** | **1000** | [OpenAI pricing](https://developers.openai.com/api/docs/pricing) |

## Summarization middleware chain

| Model key | USD input / 1M | USD output / 1M | Credits in / 1M | Credits out / 1M |
|-----------|----------------|-----------------|-----------------|------------------|
| `google:gemini-3.1-flash-lite` | $0.25 | $1.50 | **25** | **150** |
| `openai:gpt-4o-mini` | $0.15 | $0.60 | **15** | **60** |

## Cached input (optional v2 metering)

| Model key | USD cached / 1M | Credits cached / 1M |
|-----------|---------------|---------------------|
| `google:gemini-3.5-flash` | $0.15 | 15 |
| `google:gemini-3.1-flash-lite` | $0.025 | 3 |
| `openai:gpt-4o` | $1.25 | 125 |
| `openai:gpt-5` | $0.125 | 13 |
| `openai:gpt-4o-mini` | $0.075 | 8 |

Bill cached tokens separately when `usage_metadata` exposes them.

## Example: one orchestrator turn (Gemini primary)

Assumption: 3 agent steps, 4k input + 120 output each, no summarization.

```
Per step: (4000/1M × 150) + (120/1M × 900) ≈ 0.6 + 0.11 ≈ 1 credit
Turn total ≈ 3 credits  (~$0.03 API cost)
```

## Example: same turn on GPT-4o fallback

```
Per step: (4000/1M × 250) + (120/1M × 1000) ≈ 1 + 0.12 ≈ 2 credits
Turn total ≈ 6 credits  (~$0.06 API cost)
```

## Example: heavy multimodal turn

Assumption: 3 steps, 25k input (vision) + 200 output on `gemini-3.5-flash`.

```
Per step: (25000/1M × 150) + (200/1M × 900) ≈ 3.75 + 0.18 ≈ 4 credits
Turn total ≈ 12 credits  (~$0.12 API cost)
```

## Hold ceilings (defaults)

| Scope | Credits | ~USD API ceiling |
|-------|---------|------------------|
| Orchestrator turn | 150 | ~$1.50 |
| MedGemma job | 200 | ~$2.00 |
| Min balance to start | 150 | — |

Tune from LangSmith p95 after Phase 4 shadow metering.

## Notes

- **Gemini 3.5 Flash output** includes thinking tokens when thinking mode is on — billed at output rate ($9/1M).
- **gpt-5** in the fallback chain is the standard short-context tier ($1.25/$10); long-context is 2× input on OpenAI.
- Override any rate via env vars listed in `.env_example` (prefix `BILLING_`).
