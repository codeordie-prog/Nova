## Model Reliability

MedGemma 4B Multimodal is a strong small model for medical tasks. It reports **64.4% on MedQA**, placing it among the better open models in the sub-8B parameter range. In an unblinded study, **81%** of MedGemma-generated chest X-ray reports were considered accurate enough by a U.S. board-certified radiologist to support similar patient management decisions compared to original radiology reports. It also performs competitively on medical image classification benchmarks.

## Why Structured Output Is Required

To compute confidence reliably, the model must return a consistent JSON schema. This lets us extract the exact text span used for confidence scoring.

We use two target modes:

1. **Diagnosis mode**: extract `diagnosis`.
2. **Product-review mode**: when `diagnosis` is empty, extract product fields such as `product_name`, `how_to_use`, and `safety_in_pregnancy`.

## Confidence Extraction Intuition

Transformer models produce logits for the next token. A softmax converts logits into probabilities over the vocabulary:

$$
p_i = \frac{e^{z_i}}{\sum_j e^{z_j}}
$$

Where:

- $z_i$: logit for token $i$
- $p_i$: probability of token $i$

During inference, we receive **log-probabilities** for generated tokens:

$$
\ell_i = \log p_i
$$

So token probabilities can be recovered as:

$$
p_i = e^{\ell_i}
$$

## Confidence Score Algorithm

Let the target text span (diagnosis or selected product fields) contain $m$ aligned tokens, with log-probabilities:

$$
\ell_1, \ell_2, \ldots, \ell_m
$$

### Step 1: Compute mean log-probability

$$
\bar{\ell} = \frac{1}{m} \sum_{k=1}^{m} \ell_k
$$

### Step 2: Convert back to probability space

$$
C = e^{\bar{\ell}}
$$

This is equivalent to the geometric mean of token probabilities:

$$
C = \left(\prod_{k=1}^{m} p_k\right)^{\frac{1}{m}}
$$

Where $C$ is the confidence score.

## Practical Routing Rule

- If `diagnosis` is non-empty, confidence is computed from diagnosis-token spans.
- If `diagnosis` is empty, confidence is computed from product-review spans (`product_name`, `how_to_use`, `safety_in_pregnancy`).
- If span alignment fails, we fall back to all generated tokens.

This keeps confidence scoring robust across both diagnosis and product-review outputs while using a single inference endpoint.
