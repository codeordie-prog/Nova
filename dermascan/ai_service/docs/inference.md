# Inference Optimization Analysis

> Understanding the compute/memory tradeoffs for serving MedGemma-4B on NVIDIA A10 GPUs using vLLM.

---

# Quick Summary

## Key Findings

* Theoretical compute-memory crossover: **~200 batch size**
* Practical KV-cache-limited batch size: **~50 concurrent requests**
* Real-world inference is primarily **memory-bound**
* Weight fetch amortization improves cost/token as batch increases
* KV cache growth eventually dominates memory pressure

## Recommended vLLM Parameters

```json
{
  "gpu_memory_utilization": 0.85,
  "max_model_len": 1536,
  "max_num_seqs": 50,
  "max_num_batched_tokens": 76800
}
```

---

# 1. Inference Time Lower Bound

Transformer inference is constrained by the slower of:

1. Compute throughput
2. Memory movement

Therefore:

$$
t_{inference} \ge \max(t_c, t_m)
$$

Where:

* $t_c$ = compute-bound latency
* $t_m$ = memory-bound latency

---

# 2. Compute-Bound Analysis

Inference servers process requests in batches.

For a batch size $B$:

$$
t_c = \frac{B \cdot N_{active}}{FLOPS}
$$

Where:

* $B$ = batch size
* $N_{active}$ = active parameters participating in the forward pass
* $FLOPS$ = effective hardware throughput

## Important Observation

The compute term scales linearly with batch size:

$$
t_c \propto B
$$

Larger batches increase total arithmetic work.

---

# 3. NVIDIA A10 GPU Specifications

| Component              | Value      |
| ---------------------- | ---------- |
| GPU                    | NVIDIA A10 |
| VRAM                   | 24 GB      |
| Memory Bandwidth       | 600 GB/s   |
| BF16 Tensor Throughput | 125 TFLOPS |
| Peak Tensor Throughput | 250 TFLOPS |

---

# 4. Memory-Bound Analysis

Memory latency consists of:

1. Weight fetch cost
2. KV-cache memory traffic

## Memory Equation

$$
t_m = \frac{T_{weights} + T_{KV}}{memory\ bandwidth}
$$

Where:

$$
T_{weights} = N_{weights}
$$

$$
T_{KV} = B \cdot L \cdot b_{token}
$$

Substituting:

$$
t_m = \frac{N_{weights} + B \cdot L \cdot b_{token}}{memory\ bandwidth}
$$

Where:

* $B$ = batch size
* $L$ = context length
* $b_{token}$ = token state size

---

# 5. Important Observations

## Compute Scaling

$$
t_c \propto B
$$

## Memory Scaling

$$
t_m \propto B \cdot L
$$

## Weight Fetch Cost

The weight-fetch term is approximately constant:

$$
\frac{N_{weights}}{memory\ bandwidth}
$$

This creates a lower latency floor.

---

# 6. Cost Per Token

For a batch size $B$:

* one forward pass generates one token per active sequence
* total generated tokens per step = $B$

Therefore:

$$
Cost/token = \frac{t_{inference}}{B}
$$

Using the inference lower bound:

$$
Cost/token \ge \frac{\max(t_c, t_m)}{B}
$$

---

# 7. Memory-Bound Cost/Token

Starting from:

$$
t_m = \frac{N_{weights} + B \cdot L \cdot b_{token}}{memory\ bandwidth}
$$

Define:

$$
C_w = \frac{N_{weights}}{memory\ bandwidth}
$$

$$
C_{kv} = \frac{L \cdot b_{token}}{memory\ bandwidth}
$$

Then:

$$
t_m = C_w + B \cdot C_{kv}
$$

So:

$$
\frac{t_m}{B} = \frac{C_w}{B} + C_{kv}
$$

## Interpretation

* Increasing batch size amortizes the weight-fetch cost
* $C_w/B$ shrinks as $B$ increases
* Cost/token eventually flattens toward $C_{kv}$

---

# 8. Compute-Bound Cost/Token

Starting from:

$$
t_c = \frac{B \cdot N_{active}}{FLOPS}
$$

Then:

$$
\frac{t_c}{B} = \frac{N_{active}}{FLOPS}
$$

## Interpretation

In the compute-bound regime:

* cost/token is approximately constant
* increasing batch mainly improves throughput
* latency per token no longer improves significantly

---

# 9. Combined Interpretation

Putting both regimes together:

$$
Cost/token \gtrsim \max\left(\frac{t_m}{B}, \frac{t_c}{B}\right)
$$

Where:

$$
\frac{t_m}{B} = \frac{C_w}{B} + C_{kv}
$$

$$
\frac{t_c}{B} = \frac{N_{active}}{FLOPS}
$$

## Practical Meaning

### Small Batch Sizes

* Memory-bound
* Weight fetch dominates
* Higher cost/token

### Larger Batch Sizes

* Weight fetch becomes amortized
* Cost/token decreases

### Very Large Batch Sizes

* Compute becomes dominant
* Cost/token flattens
* KV cache pressure increases significantly

---

# 10. Hardware Arithmetic Intensity Ratio

A useful hardware ratio is:

$$
\frac{FLOPS}{memory\ bandwidth}
$$

Using A10 specs:

$$
\frac{125 \times 10^{12}}{600 \times 10^9} \approx 208
$$

So the A10 can perform approximately:

$$
\boxed{208\ FLOPs/byte}
$$

This is the approximate hardware balance point between:

* compute throughput
* memory movement

---

# 11. MedGemma-4B Crossover Analysis

## Model Specifications

| Property            | Value |
| ------------------- | ----- |
| Parameters          | 4B    |
| Precision           | BF16  |
| Bytes per parameter | 2     |
| Approx weight size  | 8 GB  |

---

## Step 1 — Weight Fetch Lower Bound

Weight size:

$$
4 \times 10^9 \times 2 = 8 \times 10^9\ bytes
$$

Weight fetch latency:

$$
C_w = \frac{8 \times 10^9}{600 \times 10^9}
$$

$$
C_w \approx 0.0133\ sec
$$

So:

$$
\boxed{13.3\ ms}
$$

This is the theoretical memory latency floor.

---

## Step 2 — FLOPs Per Token

A transformer forward pass performs approximately:

* one multiplication
* one addition

per parameter.

Therefore:

$$
FLOPs/token \approx 2 \times N_{params}
$$

For MedGemma-4B:

$$
FLOPs/token \approx 2 \times 4 \times 10^9
$$

$$
FLOPs/token \approx 8 \times 10^9
$$

So:

$$
\boxed{8\ GFLOPs/token}
$$

---

## Step 3 — Compute Time Per Token

$$
t_c/token = \frac{8 \times 10^9}{125 \times 10^{12}}
$$

$$
t_c/token \approx 6.4 \times 10^{-5}\ sec
$$

So:

$$
\boxed{64\ \mu s/token}
$$

---

## Step 4 — Idealized Crossover Batch Size

Ignoring KV-cache growth temporarily:

$$
\frac{C_w}{B} = t_c/token
$$

Therefore:

$$
B = \frac{0.0133}{6.4 \times 10^{-5}}
$$

$$
B \approx 208
$$

So the idealized crossover batch size is:

$$
\boxed{B_{ideal} \approx 200}
$$

---

# 12. Why This Crossover Is Not Physically Reachable

The value:

$$
B \approx 200
$$

is only an idealized weight-amortization crossover.

It assumes:

* negligible KV-cache cost
* infinite VRAM
* perfect scheduling
* no fragmentation
* no attention overhead

Real systems violate these assumptions.

---

# 13. KV Cache Growth

KV cache is significantly larger than a few bytes per token.

For Grouped Query Attention (GQA):

$$
KV/token = 2 \cdot N_{layers} \cdot N_{kv\ heads} \cdot d_{head} \cdot bytes
$$

For MedGemma-4B:

| Parameter      | Value |
| -------------- | ----- |
| Layers         | 34    |
| KV Heads       | 4     |
| Head Dimension | 256   |
| Precision      | BF16  |

Per-token KV cache:

$$
2 \cdot 34 \cdot 4 \cdot 256 \cdot 2
$$

$$
= 139,264\ bytes
$$

$$
\approx 136\ KB/token
$$

---

# 14. Per-Request KV Cache

Assume:

$$
L = 1024
$$

Then:

$$
136\ KB \times 1024
$$

$$
\approx 136\ MB/request
$$

To allow for context variation and fragmentation:

$$
\boxed{200\ MB/request}
$$

is a safer practical estimate.

---

# 15. Practical Batch Limit on A10

## Available VRAM Budget

| Component           | Memory |
| ------------------- | ------ |
| Total VRAM          | 24 GB  |
| Safe utilization    | 20 GB  |
| Model weights       | 8 GB   |
| CUDA overhead       | ~2 GB  |
| Remaining KV budget | ~10 GB |

---

## Maximum Practical Concurrent Requests

$$
\frac{10\ GB}{200\ MB}
$$

$$
\approx 50
$$

So:

$$
\boxed{max_num_seqs \approx 50}
$$

---

# 16. Final Interpretation

This reveals an important transformer-serving property:

* Larger batches amortize weight-fetch cost
* KV cache grows linearly with batch size and sequence length
* Memory pressure dominates before full compute saturation

Therefore:


With a given Hardware configuration with respect to Batch size sensitivity below crossover point, Inference Cost in Transformers is usually memory-bound in practice


---

# 17. Recommended vLLM Parameters

## `gpu_memory_utilization`

```json
0.85
```

Leaves memory headroom for:

* CUDA runtime
* fragmentation
* scheduling overhead

---

## `max_model_len`

```json
1536
```

Breakdown:

* 1024 prompt/context tokens
* 512 generation tokens

---

## `max_num_seqs`

```json
50
```

Derived from KV-cache memory limits.

---

## `max_num_batched_tokens`

```json
76800
```

Derived from:

$$
50 \times 1536 = 76800
$$

This allows vLLM to safely process worst-case batches.

> **Note:** sections 1–17 above model inference as **static-batch transformer
> serving**. vLLM is not that engine. Sections 18+ re-frame the same workload
> in vLLM-native terms (continuous batching, PagedAttention, prefill vs decode,
> multimodal cost, prefix caching, etc.). The earlier numbers remain useful as
> a *worst-case envelope*; the sections below describe what actually happens
> at runtime in `app.py`.

---

# 18. vLLM Engine Reality Check

The cost model in §1–§16 assumes:

* a fixed batch `B` per forward pass
* full per-request KV reservation (`L · b_token`)
* text-only inputs

vLLM violates all three. The differences matter for capacity planning.

## 18.1 Continuous (in-flight) batching

vLLM does **not** process a fixed cohort. At every decode step it dynamically
mixes:

* sequences in **prefill** (large token chunks)
* sequences in **decode** (1 token per sequence)

Therefore "batch size $B$" in §2–§9 maps to two distinct quantities in vLLM:

* `max_num_seqs` — concurrency ceiling (decode-side)
* `max_num_batched_tokens` — per-step token budget (prefill-side)

These two parameters control **different bottlenecks** and should not be tuned
together as if they were one number.

## 18.2 Prefill vs decode regimes

| Regime  | Cost driver         | Governed by                | Latency metric    |
|---------|---------------------|----------------------------|-------------------|
| Prefill | Compute (FLOPs)     | `max_num_batched_tokens`   | TTFT              |
| Decode  | Memory bandwidth    | `max_num_seqs`, KV pressure | Inter-token latency |

The §1–§9 memory-bound conclusion applies cleanly to **decode**. Prefill is
its own latency story driven by prompt length × batched-token budget.

---

# 19. PagedAttention and KV Memory Reality

## 19.1 Allocation is paged, not flat

§14 reserves `~200 MB/request` flat. vLLM doesn't do that.

PagedAttention allocates KV in fixed-size **blocks** (default `block_size=16`
tokens). Memory is consumed only for tokens actually generated, plus a small
fragmentation overhead.

For MedGemma-4B (`136 KB/token`):

* a request that emits 200 tokens consumes:
  $$200 \times 136\ \text{KB} \approx 27\ \text{MB}$$
* a request that emits 1024 tokens consumes:
  $$1024 \times 136\ \text{KB} \approx 136\ \text{MB}$$

So §15's "≈ 50 concurrent requests" is the **worst-case envelope** for full
`max_model_len` usage. The realistic steady-state ceiling is typically
**higher** for short outputs and **lower** only if every request fills the
context window.

## 19.2 Block size trade-off

`--block-size` (default 16):

* smaller block → less internal fragmentation, more block-table overhead
* larger block (32) → fewer block-table ops, more wasted tail tokens

Default 16 is a good balance for our payloads. Worth revisiting only if
profiling shows block-table overhead.

## 19.3 KV dtype lever

`--kv-cache-dtype fp8` roughly **halves** KV memory:

* doubles realistic `max_num_seqs` ceiling
* small accuracy cost for most tasks
* not yet enabled in `app.py` — kept as a future capacity lever

---

# 20. Multimodal Cost (the doc previously ignored)

MedGemma is a vision-language model. §11's "Model weights ≈ 8 GB" only counts
the LM. Real picture:

| Component            | Approx VRAM |
|----------------------|-------------|
| LM weights (BF16)    | ~8 GB       |
| Vision tower (SigLIP-style) | ~0.4–0.8 GB |
| CUDA + graphs        | ~2–3 GB     |
| KV cache             | remainder   |

## 20.1 Image token budget

Each image expands into **N visual tokens** (model-specific, typically
256–576) that occupy `max_model_len` and KV.

Effective prompt length per request:

$$
L_{eff} = L_{text} + L_{image}
$$

With our `_STRUCTURED_SYSTEM_PROMPT` (large) + image (256–576 tokens) +
user prompt, baseline `L_{eff}` is often **600–1200 tokens before any
generation**. This is why `max_model_len = 1536` is tight; consider 2048 if
prompts grow.

## 20.2 Vision encode is a prefill spike

The vision tower runs as part of prefill. It is **compute-heavy** and
competes with decode for GPU time. This is the single biggest reason to
enable chunked prefill (§21.1).

---

# 21. Free-Win Engine Flags Not Currently Enabled

## 21.1 Chunked prefill — `--enable-chunked-prefill`

Splits long prompts into smaller prefill chunks scheduled across steps so
decode requests are not blocked by a giant prefill burst.

Why it matters here:

* Our system prompt is large.
* Vision encode adds a prefill spike.
* Without chunked prefill, P99 inter-token latency for in-flight decodes
  spikes whenever a new request lands.

Pair with a smaller `max_num_batched_tokens` (e.g. 4096–8192) to keep
prefill chunks bounded.

## 21.2 Prefix caching — `--enable-prefix-caching`

Reuses KV blocks for shared prompt prefixes.

Why it is essentially free for this workload:

* Every request shares the same `_STRUCTURED_SYSTEM_PROMPT`.
* Every request shares the same task framing.
* That prefix is large.

Effect:

* Lower TTFT on warm prefixes.
* Saved KV memory translates into higher achievable `max_num_seqs`.

**Recommendation:** enable both `--enable-chunked-prefill` and
`--enable-prefix-caching` in `_start_vllm_server` cmd args.

---

# 22. `max_num_batched_tokens` — Correct Derivation

§17 derives `76800 = 50 × 1536` as a "worst-case batch". That is **not** what
this flag does in vLLM.

Correct framing:

* `max_num_batched_tokens` is the **per-step token budget**, primarily for
  prefill scheduling.
* Decode steps consume at most `max_num_seqs` tokens (1 per sequence) — far
  below 76800.
* A larger value → faster TTFT for long prompts but more head-of-line
  blocking on decode.

Recommended sizing:

| Goal                          | Suggested value     |
|-------------------------------|---------------------|
| Latency-sensitive (with chunked prefill) | 4096–8192   |
| Throughput-sensitive          | 16384–32768         |
| Current value (76800)         | works, but oversized for our SLOs |

---

# 23. Replica-Aware Deployment Ceiling

`app.py` configures Modal with:

```python
@app.cls(gpu="A10", min_containers=2, max_containers=2, ...)
```

So the system-wide concurrency ceiling is:

$$
\text{total\_concurrency} = N_{replicas} \times \text{max\_num\_seqs}
$$

With current settings: $2 \times 50 = 100$ concurrent in-flight requests
(worst case envelope).

§15's "≈ 50" is **per-replica**, not system-wide. Capacity planning should
use the deployment ceiling.

---

# 24. Hot-Path Costs Specific to This Code

## 24.1 `logprobs=True`

`_infer_one_vllm` requests `logprobs: True` (used by
`compute_confidence_score`). This:

* adds memory and serialization overhead per token,
* limits some scheduler fast paths.

Keep it on (confidence scoring is core), but never raise `top_logprobs`
above the minimum needed.

## 24.2 Guided JSON decoding

`response_format={"type": "json_object"}` engages vLLM's guided decoding
backend. It is correctness-critical here (downstream parsing) but has
measurable per-token overhead. Acceptable cost; just be aware.

## 24.3 `timeout=None` on vLLM HTTP calls

`httpx.post(..., timeout=None)` in both `_infer_one_vllm` and `run_batch`
means a stalled vLLM stalls the worker slot indefinitely. Tracked under
P0/P1 in `claude.md`. Add concrete connect/read timeouts.

---

# 25. Other vLLM Levers (Reference)

| Lever                            | When to consider                          |
|----------------------------------|-------------------------------------------|
| `--kv-cache-dtype fp8`           | Need higher `max_num_seqs`                 |
| Weight quantization (AWQ/GPTQ)   | Need to fit a larger model on A10          |
| `--enforce-eager`                | Cold-start memory pressure / debugging     |
| `--swap-space N`                 | Tolerate occasional CPU offload over OOM   |
| Speculative decoding             | Short outputs; reduce decode latency       |
| Tensor parallel (`--tp 2`)       | Multi-GPU; not relevant on single A10      |

---

# 26. Capacity Targets (to be filled with measurement)

The doc currently lacks a latency-target framing. Production tuning should
work backwards from SLOs, not forwards from memory math.

Suggested table to populate via load test:

| Metric                          | Target    | Observed |
|---------------------------------|-----------|----------|
| P50 TTFT (text-only)            | < 500 ms  | TBD      |
| P95 TTFT (with image)           | < 2.5 s   | TBD      |
| P95 inter-token latency         | < 60 ms   | TBD      |
| Sustained QPS @ SLO             | TBD       | TBD      |
| Per-replica `max_num_seqs` cap  | 50        | TBD      |

---

# 27. Summary of What Changed in This Revision

1. Static-batch model is acknowledged as a worst-case envelope, not the
   runtime model.
2. PagedAttention and continuous batching are now first-class.
3. Prefill vs decode regimes are separated.
4. Multimodal cost (vision tower + image tokens) is explicit.
5. Chunked prefill and prefix caching are recommended as free wins.
6. `max_num_batched_tokens` is re-derived in the prefill-budget framing.
7. Deployment-level concurrency = `replicas × max_num_seqs`.
8. Hot-path costs (`logprobs`, guided JSON, infinite HTTP timeout) are
   called out.
9. Future levers (FP8 KV, quantization, speculative decoding, swap) are
   listed without prescribing.
