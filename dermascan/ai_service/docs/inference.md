# Inference Optimization Analysis

This document analyzes the optimal inference serving configuration for MedGemma-4B on NVIDIA A10 GPUs, determining safe vLLM parameters based on compute/memory bounds and KV cache constraints.

## Quick Summary

**Key Findings:**
- Theoretical compute-memory crossover: 208-417 concurrent requests
- Practical KV cache limit: 50 concurrent requests
- Inference is **memory-bound** in practice
- Safe vLLM parameters: `max_num_seqs=50`, `max_num_batched_tokens=76800`, `gpu_memory_utilization=0.85`

**Configuration File:**
- `dermascan/ai_service/modal/model_config/config.json`

---

## Analysis of the optimal inference serving
Serving open source models requires a deeper low level understanding inorder to setup a scalable reliable architecture.
Inference is bound by either of the two:

1. Compute bound \(t_c\): time taken to perform matrix multiplications.
2. Memory bound \(t_m\): weight fetch + KV cache fetch time.

## Inference time analysis

$$
t_{\text{inference}} \ge \max(t_c, t_m)
$$

- Inference latency is lower-bounded by the slower term between compute and memory.

## Compute bound analysis

- A reliable, scalable architecture serves inference in batches.
- For a given Batch, we perform Mat * Mat FLOPs against the Total active parameters divided by
the Hardware capacity (FLOPS).

$$
t_c = \frac{B \cdot N_{\text{active}}}{\text{FLOPS}}
$$

- A FLOP is the number of Floating point operations a given hardware can perform per sec under a particular precision per sec.

- The compute term is linear with respect to Batch size.

## NVIDIA A10 GPU Specifications

- BFLOAT16 Tensor Core - 125 teraFLOPS | 250 teraFLOPS
- GPU memory - 24GB
- GPU memory bandwidth - 600GB/sec

## Memory bound analysis

$$
t_m = \frac{T_{\text{weights}} + T_{\text{KV}}}{\text{memory bandwidth}}
$$

$$
T_{\text{weights}} = N_{\text{weights}}
$$

$$
T_{\text{KV}} = B \cdot L \cdot b_{\text{token}}
$$

- Therefore the equation becomes:

$$
t_m = \frac{N_{\text{weights}} + B \cdot L \cdot b_{\text{token}}}{\text{memory bandwidth}}
$$

- The KV cache term is linear with respect to the batch size and context length.
- The weight fetch term is constant with respect to size of the model

Where:

B - Batch size.
L - Context length.
b - size of each token.

## Observation so far

- The compute bound term is linear with respect to the batch size.
- The memory bound term is linear with respect to the batch size and context length.
- The weight fetch term is constant with respect to size of the model and memory bandwidth.
- The maximum between the sum(KV cache and weight fetch term) and compute bound term defines a latency lower bound.

- A small size batch - inference is memory bound.
- A large size batch - inference is compute bound.

## Cost per token and Batch sensitivity.
For a given batch size \(B\), one forward pass produces one token per active sequence, i.e. \(B\) tokens.

$$
\text{Cost/token} = \frac{t_{\text{inference}}}{B}
$$

Using the latency lower bound:
$$
t_{\text{inference}} \ge \max(t_c, t_m)
$$

Therefore:
$$
\text{Cost/token} \ge \frac{\max(t_c, t_m)}{B}
$$

- Cost per token is inversely proportional to batch size \(B\).
- Increasing \(B\) reduces cost per token due to amortization.
- Cost per token is still sensitive to context length \(L\) and model size.

## Cost per token in the memory-bound regime

Starting from:

$$
t_m = \frac{N_{\text{weights}} + B \cdot L \cdot b_{\text{token}}}{\text{memory bandwidth}}
$$

Define:

$$
C_w = \frac{N_{\text{weights}}}{\text{memory bandwidth}}, \quad
C_{kv} = \frac{L \cdot b_{\text{token}}}{\text{memory bandwidth}}
$$

Then:

$$
t_m = C_w + B\,C_{kv}
$$

So memory-bound cost per token is:

$$
\frac{t_m}{B} = \frac{C_w}{B} + C_{kv}
$$

- As \(B\) increases, \(C_w/B\) shrinks.
- The curve flattens toward \(C_{kv}\), which is the asymptotic floor.

## Memory-bound cost/token graph

| Batch size \(B\) | Cost/token (normalized) |
|---:|---:|
| 1  | 2.00 |
| 2  | 1.10 |
| 4  | 0.65 |
| 8  | 0.43 |
| 16 | 0.31 |
| 32 | 0.26 |
| 64 | 0.23 |

```text
Memory-bound Cost/token vs Batch Size

B=1   | ######################################## (2.00)
B=2   | ######################                   (1.10)
B=4   | #############                            (0.65)
B=8   | #########                                (0.43)
B=16  | ######                                   (0.31)
B=32  | #####                                    (0.26)
B=64  | ####                                     (0.23)
```

Example normalized form:

$$
\text{Cost/token} = \frac{2}{B} + 0.2
$$

## Cost per token in the compute-bound regime

From compute latency:

$$
t_c = \frac{B \cdot N_{\text{active}}}{\text{FLOPS}}
$$

Compute-bound cost per token is:

$$
\frac{t_c}{B} = \frac{N_{\text{active}}}{\text{FLOPS}}
$$

Interpretation:
- In the compute-bound regime, cost/token is approximately constant with batch size.
- Increasing batch size mostly improves throughput, not per-token compute cost.

## Compute-bound cost/token graph

| Batch size \(B\) | Cost/token (normalized) |
|---:|---:|
| 1  | 0.80 |
| 2  | 0.80 |
| 4  | 0.80 |
| 8  | 0.80 |
| 16 | 0.80 |
| 32 | 0.80 |
| 64 | 0.80 |

```text
Compute-bound Cost/token vs Batch Size

B=1   | ################ (0.80)
B=2   | ################ (0.80)
B=4   | ################ (0.80)
B=8   | ################ (0.80)
B=16  | ################ (0.80)
B=32  | ################ (0.80)
B=64  | ################ (0.80)
```

Example normalized form:

$$
\text{Cost/token} = 0.8
$$

## Combined interpretation (memory + compute)

Putting both regimes together:

$$
\text{Cost/token} \gtrsim \max\left(\frac{t_m}{B}, \frac{t_c}{B}\right)
$$

with

$$
\frac{t_m}{B} = \frac{C_w}{B} + C_{kv}, \quad
\frac{t_c}{B} = \frac{N_{\text{active}}}{\text{FLOPS}}
$$

- For small batch sizes, memory amortization dominates (higher cost/token).
- As batch size increases, the \(C_w/B\) part shrinks and memory cost approaches \(C_{kv}\).
- Beyond a crossover point, compute cost can become the dominant floor.
- So, with sufficient batch increase, compute can eventually define cost/token.

## Batch size sensitivity and determining the Cross-over point.

To determine the crossover point where compute cost equals memory cost, set:

$$
t_c = t_m
$$

Using the weight-fetch-amortized memory term:

$$
\frac{B \cdot N_{\text{active}}}{\text{FLOPS}} = \frac{N_{\text{weights}}}{\text{memory bandwidth}}
$$

Rearrange to isolate hardware terms on one side:

$$
\frac{\text{FLOPS}}{\text{memory bandwidth}} = B \cdot \frac{N_{\text{weights}}}{N_{\text{active}}}
$$

## Solve hardware ratio with A10 specs

Using A10 GPU specifications:
- Memory bandwidth: \(600 \times 10^9\) bytes/s
- Tensor Core throughput: \(125 \times 10^{12}\) FLOPS (conservative) and \(250 \times 10^{12}\) FLOPS (peak)

For conservative FLOPS (125 TFLOPS):

$$
\frac{\text{FLOPS}}{\text{memory bandwidth}} = \frac{125 \times 10^{12}}{600 \times 10^9} \approx 208.3
$$

For peak FLOPS (250 TFLOPS):

$$
\frac{\text{FLOPS}}{\text{memory bandwidth}} = \frac{250 \times 10^{12}}{600 \times 10^9} \approx 416.7
$$

So the hardware ratio is approximately \(208\) to \(417\) for the A10.

## Solve crossover batch size with MedGemma-4B specs

Using MedGemma-4B specifications:
- Model size: 4B parameters
- Precision: BF16 (2 bytes per parameter)
- Each parameter performs 2 FLOPs per forward pass (addition + multiplication)

Compute time (accounting for 2 FLOPs per parameter):

$$
t_c = \frac{B \cdot 2 \cdot N_{\text{params}}}{\text{FLOPS}}
$$

Memory time (converting parameters to bytes):

$$
t_m = \frac{N_{\text{params}} \cdot \text{bytes\_per\_param}}{\text{memory\_bandwidth}}
$$

Setting \(t_c = t_m\):

$$
\frac{B \cdot 2 \cdot N_{\text{params}}}{\text{FLOPS}} = \frac{N_{\text{params}} \cdot \text{bytes\_per\_param}}{\text{memory\_bandwidth}}
$$

For BF16, bytes\_per\_param = 2:

$$
\frac{B \cdot 2 \cdot N_{\text{params}}}{\text{FLOPS}} = \frac{N_{\text{params}} \cdot 2}{\text{memory\_bandwidth}}
$$

Cancel params and the factor of 2:

$$
\frac{B}{\text{FLOPS}} = \frac{1}{\text{memory bandwidth}}
$$

Rearrange to isolate hardware terms:

$$
\frac{\text{FLOPS}}{\text{memory bandwidth}} = B
$$

So the crossover batch size is simply the hardware arithmetic intensity ratio — the model size cancels entirely.

Using A10 GPU specifications:
- Memory bandwidth: \(600 * 10^9\) bytes/s
- Tensor Core throughput: \(125 * 10^{12}\) FLOPS (conservative) and \(250 * 10^{12}\) FLOPS (peak)

For conservative FLOPS (125 TFLOPS):

$$
B = \frac{125 \times 10^{12}}{600 \times 10^9} \approx 208.3
$$

For peak FLOPS (250 TFLOPS):

$$
B = \frac{250 \times 10^{12}}{600 \times 10^9} \approx 416.7
$$

So the crossover batch size for MedGemma-4B on A10 is approximately \(208\) (conservative) to \(417\) (peak).

This can be interpreted in simple terms as follows:
- With a conservative batch size of 208 prompts, the model will be memory-bound (limited by memory bandwidth).
- With a batch size of 417 prompts, the model will be compute-bound (limited by computational capacity).
- However, we are more likely to hit memory capacity before we hit compute capacity, so even though the theoretical conservative batch size for the crossover point is 208 prompts, we cannot set the batch size to 208 prompts,
we have to find a sweet spot that avoids hitting memory capacity while still utilizing the computational resources effectively.

## Finding the inference engine cmd parameters.

- This part allows us to find the optimal batch size for the inference engine. 
- Since we are more likely to hit memory capacity before we hit compute capacity, its good to understand that our inference will be memory bound.

### GPU Utilization parameter
- We will cap this parameter to `0.85` to avoid hitting memory floor.

### Available vRAM - NVIDIA A10 GPU
- Total vRAM: 24 GB
- We will use 20 GB.

### Model loading + CUDA Overhead and KV Cache budget
- dtype=bf16 - `2 bytes` per parameter
- CUDA overhead approximately `2 GB`
- Model size - 2 bytes * 4 Billion parameters = `8GB`
- Total - 10GB (Non-negotiable)
- Available vRAM = 20GB - 10GB = `10GB` 
- Therefore: our KV cache budget is `10GB`.

### KV cache linear scaling.
- KV Cache scales linearly per request.
- The cost is approximated as follows:

For a transformer with Grouped Query Attention (GQA), KV cache per token is:

$$
\text{KV cache per token} = 2 \cdot N_{\text{layers}} \cdot N_{\text{kv\_heads}} \cdot d_{\text{head}} \cdot \text{bytes\_per\_param}
$$

Where:
- \(N_{\text{layers}}\) = number of transformer layers
- \(N_{\text{kv\_heads}}\) = number of key-value heads (GQA shares these across query heads)
- \(d_{\text{head}}\) = head dimension
- bytes_per_param = 2 (for BF16)
- Factor of 2 accounts for both K and V matrices

For MedGemma-4B (Gemma 3 4B architecture):
- \(N_{\text{layers}} = 34\)
- \(N_{\text{kv\_heads}} = 4\)
- \(d_{\text{head}} = 256\)

Per token KV cache:

$$
2 \cdot 34 \cdot 4 \cdot 256 \cdot 2 = 139,264 \text{ bytes} \approx 136 \text{ KB}
$$

Per request (assuming average context length \(L = 1024\) tokens):

$$
136 \text{ KB} \cdot 1024 = 139,264 \text{ KB} \approx 136 \text{ MB}
$$
but lets cap it at 200MB due to context length variation.

With 10GB KV cache budget:

$$
\frac{10 \text{ GB}}{200 \text{ MB}} \approx 50 \text{ concurrent requests}
$$

Therefore, the practical batch size is limited by KV cache memory to approximately 50 requests, which is below the theoretical compute-memory crossover point of 208. This confirms that our inference will be memory-bound in practice.

## Setting inference engine parameters

Based on the above analysis, we can set the following parameters for the inference engine:

### vLLM Configuration (config.json)

**`gpu_memory_utilization: 0.85`**
- Caps GPU memory usage to avoid hitting memory floor
- Leaves headroom for CUDA operations and stability

**`max_model_len: 1536`**
- Maximum sequence length the model can process
- 1024 context + 512 max_new_tokens
- Provides headroom for context length variations

**`max_num_seqs: 50`**
- Maximum concurrent sequences (batch size)
- Derived from: 10 GB KV budget / 200 MB per request
- Practical limit due to KV cache memory constraints
- Below theoretical crossover (208), confirming memory-bound inference

**`max_num_batched_tokens: 76800`**
- Maximum total tokens vLLM processes in a single batch
- Calculated as: 50 sequences × 1536 max tokens
- Ensures vLLM can handle worst-case batch without exceeding memory
