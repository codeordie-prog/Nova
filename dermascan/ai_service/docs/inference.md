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

$$
\boxed{
Transformer\ inference\ is\ usually\ memory-bound\ in\ practice
}
$$

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
