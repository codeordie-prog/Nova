# IP protection plan — hosting DermaScan for a medical partner (Azure)

Research + recommended architecture for protecting DermaScan source code and
proprietary logic when partnering with a medical facility that requires hosting
in **their Microsoft Azure** environment.

**Status:** research / decision doc (no implementation yet).
**Driver confirmed with stakeholders:** the partner's requirement is **patient-data
residency / compliance**, *not* a demand to own or inspect our code.

---

## 0. TL;DR

- You **cannot** make Python source truly unreadable to a party that controls the
  hardware it runs on. Obfuscation and compilation only raise reverse-engineering
  cost; they are a **speed bump, not a lock**.
- Because the partner's real need is **data residency**, the strongest *and*
  simplest answer is **architectural**: keep the DermaScan "brain" (orchestration,
  rules engine, prompts, algorithms) on **our** infrastructure and keep **patient
  data** in **their** Azure tenant. They never receive the code.
- The one subtlety to pin down in the contract: whether residency covers only
  **data at rest** (easy — Option A) or also **data in use / processing** (then
  escalate the brain into their tenant inside a **hardware TEE** — Option B,
  Azure Confidential Computing).
- Whatever we do, add **defense-in-depth** (obfuscation/compilation of crown-jewel
  modules) and a **legal perimeter** (on-prem license with no-reverse-engineering,
  audit, deletion-on-termination). Secrets never ship in the image.

---

## 1. Threat model and what we are protecting

The partner controls the Azure subscription. The realistic threat is **not** an
external attacker — it is that the partner (or their admins/contractors) could
read our disk, memory, or container image and extract proprietary logic.

### What is already OFF our box (not exposed)

| Asset | Where it runs | Exposure |
|-------|---------------|----------|
| MedGemma vision model | **Modal** (external GPU) | None — never ships to partner |
| LLM reasoning | **OpenAI / Gemini / Vertex** APIs | None — remote API calls |

### What would live near the partner (the crown jewels)

| Asset | Module(s) | Why it matters |
|-------|-----------|----------------|
| Orchestration / routing | `model_chain_routing.py`, `model_fallback_chain.py`, circuit breakers | Core differentiator |
| Agent prompts & personas | `agents_config.json`, `soul.md`, `awareness.md`, `heartbeat.md` | Prompt IP is **data**, not just code |
| Rules engine + CIR data | `rules_engine/engine/rules_engine.py`, vector store, CIR corpus | Curated safety dataset |
| Confidence / escalation | `confidence/decision_confidence.py`, `postprocessing/escalation.md` | Proprietary algorithm |
| Billing / tokenomics | `billing/tokenomics.md`, billing services | Commercial logic |
| Secrets | API keys, Redis/Postgres URLs, Modal tokens | **Must never be baked into any image** |

Note that several crown jewels (prompts, rules corpus) are **configuration/data**,
not compiled code — obfuscating code does not protect them. They must be
encrypted at rest and only materialized in a trusted memory space.

---

## 2. The four protection layers (ranked by real protection)

### Layer 1 — Architectural: don't give them the brain (STRONGEST)

Keep orchestration + rules + prompts on **our** infrastructure; expose only a
network **API/SDK** to the partner's Azure environment. They cannot reverse what
they never receive.

- **Protection:** maximum.
- **Fit for us:** excellent, *because the driver is data residency, not code control.*

### Layer 2 — Azure Confidential Computing (TEE) — if it MUST run in their tenant

Deploy into **their** Azure but inside a **hardware Trusted Execution Environment**
(AMD SEV-SNP), so the host OS, hypervisor, and their admins cannot read the
container's memory or image. Pattern: **encrypted container image + attestation-gated
key release**:

1. Encrypt image layers with a symmetric key (`skopeo` / ocicrypt).
2. Store the key in **Azure Key Vault under a Secure Key Release (SKR) policy we
   control**.
3. The enclave emits an **AMD SEV-SNP attestation report**; **Microsoft Azure
   Attestation (MAA)** verifies the hardware + the enforced policy hash
   (`CCE policy` in `HostData`).
4. Only on success does Key Vault release the key **into the enclave**, which
   decrypts layers transparently. The partner never sees plaintext image or key.

Deployment choices: **Confidential Containers on ACI** (lift-and-shift, per-group
memory encryption — easiest fit for our FastAPI + workers), **Confidential
Containers on AKS** (Kata/CoCo, if we want a cluster with Redis + multiple
workers), or **Confidential VMs** (whole-stack, coarser).

- **Protection:** very high — the only approach that runs in *their* tenant yet
  keeps code/data confidential from the host.
- **Caveats:** real migration (we are currently GCP Cloud Run); AMD SEV-SNP SKU /
  region availability; **we** must operate the Key Vault + attestation for the
  guarantee to hold. External API calls (OpenAI/Gemini/Modal) still work from
  inside the enclave.

### Layer 3 — Obfuscation / compilation (defense-in-depth)

Use *in addition to* Layer 1/2, never as the sole defense.

| Tool | Does | Strength | Honest limits |
|------|------|----------|---------------|
| **Nuitka** | Compiles whole app Python→C→native | Best whole-app; no `.pyc` to decompile | Native code still disassemblable; **all string constants/URLs/module names leak** (2026 constants dumpers extract them) |
| **Cython** | Compiles selected modules to `.so`/`.pyd` | Great for a few crown-jewel modules; faster | Async/complex generators fall back to `.pyc`; **string literals still readable**; per-OS/Python-version builds |
| **PyArmor 9.x** | Bytecode obf + RFT rename + BCC (fn→C) + `--mix-str` string encryption + anti-debug | Purpose-built for FastAPI/Django serving; **actually encrypts sensitive strings** | Public static-unpack tools exist for 8.0–9.2.x; needs interpreter/`pyarmor_runtime` binaries themselves protected (codesign/VMProtect) |

**Recommended if used:** PyArmor `--enable-rft` + `--mix-str` (filtered to prompts,
keys, SQL) on the FastAPI services, plus **Cython/Nuitka on genuine algorithm
modules** (confidence scoring, model routing, rules engine core). Keep prompts/rules
as encrypted data, decrypted only in memory (ideally inside a Layer 2 enclave).

### Layer 4 — Legal + operational (always, cheap, effective)

- **On-prem / self-hosted license** (not a code sale): retained IP ownership;
  **no reverse engineering / decompilation / modification**; confidentiality;
  **audit rights**; deployment restricted to a **named environment**; **deletion /
  return on termination**.
- **Secrets never in the image** — load from Key Vault / env at runtime.
- Where allowed, frame the deliverable as **hosted access to functionality**, not a
  copy of the software.

---

## 3. Recommended architecture (given: driver is data residency)

### The residency nuance that decides everything

"Data residency" has two possible scopes — confirm which one the contract means:

- **Data at rest only** → **Option A** below. Simple and strong.
- **Data in use / processing too** → **Option B** (brain in their tenant, inside a TEE).

### Option A — Split plane: their data, our brain (RECOMMENDED default)

Patient data **at rest** stays entirely in the partner's Azure tenant. The
orchestration brain stays on **our** infrastructure and is reached over a secure,
authenticated link. They never receive our code.

```
Partner's Azure tenant (their control)         Our infrastructure (our control)
┌──────────────────────────────────────┐       ┌───────────────────────────────┐
│ Patient data at rest:                 │       │ DermaScan "brain" (our code)  │
│  - Azure Blob (images/media)          │       │  - FastAPI API                │
│  - Postgres (rules data / records)    │       │  - orchestrator + workers     │
│  - Redis (queues / checkpoints)       │◄─────►│  - rules engine + prompts     │
│  - thin ingress / de-identification   │ mTLS  │  - confidence / routing logic │
└──────────────────────────────────────┘ VPN/  └──────────────┬────────────────┘
                                          PrivateLink          │ remote APIs
                                                                ▼
                                                   OpenAI / Gemini / Modal
```

- Data stores and ingress live in **their** tenant (residency-at-rest satisfied).
- Connect over **Azure Private Link / VPN + mTLS**; optional de-identification at
  their edge before anything transits.
- **Protection of our IP:** maximum — code never leaves us.
- **Decide:** whether transient processing on our side is acceptable under their
  residency/BAA terms. If yes → ship this. If no → Option B.

### Option B — Brain in their tenant, but inside a TEE (if processing must stay in-tenant)

Run the brain in the partner's Azure, but as **Confidential Containers (AMD
SEV-SNP) with an encrypted image and attestation-gated key release** that **we**
operate (Layer 2). Residency *and* IP confidentiality both hold, at the cost of an
Azure confidential-computing migration.

### Both options also get

- **Layer 3** obfuscation/compilation on crown-jewel modules (belt-and-suspenders;
  especially important for Option B where the image sits in their tenant).
- **Layer 4** license + operational controls.
- Secrets in **Azure Key Vault**, injected at runtime, never in the image.

---

## 4. Decision checklist (next actions, no app code yet)

| # | Action | Owner | Unblocks |
|---|--------|-------|----------|
| 1 | Confirm residency scope: **at rest** vs **in use** (get it in writing) | Legal / partner | Option A vs B |
| 2 | Confirm whether transient processing on our infra is contractually OK | Legal | Option A viability |
| 3 | Draft on-prem/self-hosted **license + confidentiality + audit** terms (Layer 4) | Legal | Any option |
| 4 | If Option A: design **Private Link / VPN + mTLS** between tenants; data stores in their Azure | Us + partner IT | Deploy A |
| 5 | If Option B: validate **AMD SEV-SNP SKU + region** in their subscription; plan ACI vs AKS confidential; stand up **our** Key Vault SKR + MAA | Us | Deploy B |
| 6 | Pick crown-jewel modules for **Layer 3** (PyArmor RFT/mix-str + Cython/Nuitka) | Us | Defense-in-depth |
| 7 | Confirm **no secret** is ever baked into an image; all via Key Vault/env | Us | Any option |

---

## 5. Honest limitations (set expectations)

- Obfuscation/compilation (Layer 3) **will not** stop a determined party that
  controls the box. Strings, URLs, prompts, and module names leak from Nuitka/Cython
  binaries; PyArmor bytecode has public static-unpackers. It only buys time.
- Only **Layer 1** (we keep the brain) or **Layer 2** (hardware TEE we attest) give
  a real confidentiality guarantee on infrastructure we don't own.
- The **license + audit** perimeter (Layer 4) is what turns "hard to steal" into
  "legally costly to steal" — it is not optional.

---

## 6. Sources

- Azure Confidential Containers overview — https://learn.microsoft.com/en-us/azure/confidential-computing/confidential-containers
- Confidential containers on ACI — https://learn.microsoft.com/en-us/azure/container-instances/container-instances-confidential-overview
- Choosing confidential container offerings (ACI vs AKS vs SGX) — https://learn.microsoft.com/en-us/azure/confidential-computing/choose-confidential-containers-offerings
- Secure Key Release with Azure Key Vault + Confidential Containers — https://learn.microsoft.com/en-us/azure/confidential-computing/skr-flow-confidential-containers-azure-container-instance
- Attestation concepts (MAA, CCE policy, HostData) — https://learn.microsoft.com/en-us/azure/container-instances/confidential-containers-attestation-concepts
- Encrypted OCI images in a TEE — https://confidentialcontainers.org/docs/features/encrypted-images/
- PyArmor security/performance options — https://pyarmor.readthedocs.io/en/latest/how-to/security.html
- PyArmor runtime-memory protection limits — https://pyarmor.readthedocs.io/en/stable/how-to/protection.html
- Reverse-engineering Nuitka (constants dumper) — https://rrock.dev/posts/2026/08/nuitka-constants-unpacker/
- On-prem vs SaaS legal considerations — https://loganpartners.com/software-distribution-models-legal-considerations-for-technology-businesses/
