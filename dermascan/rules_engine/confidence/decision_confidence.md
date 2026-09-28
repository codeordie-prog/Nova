# Decision Confidence for Product Audits

**Qutis AI Research — product rules-engine scoring (v1)**

This note defines how we combine intake context, personalization mode, ingredient evidence, and vision-derived audit confidence into a single decision confidence score $D_c \in [0, 1]$, and how $D_c$ drives user-facing guidance tone. It complements the [confidence estimation framework for skin audits](https://qutis-health.africa/research/papers/confidence-estimation-framework), which derives vision confidence from token log-probabilities. Here the focus is cosmetic product review after label read and rules enrichment.

> **Preview:** Open **Markdown Preview** (`Ctrl+Shift+V` or side-by-side `Ctrl+K V`). Equations use `$…$` / `$$…$$` and need `markdown.math.enabled` (on in Cursor settings).

---

## 1. Building User Context

Before a skin snapshot is registered, the agent persists structured profile facts through intake (goals, allergies, current products, skin type, pregnancy, breastfeeding, and related notes). An optional skin image review adds a structured audit (concern, condition signals, derived needs). Together, intake and audit supply the inputs that later become the **context score** $C$; they are normalized on $[0, 1]$ when the rules engine runs.

### Three scalars

| Symbol | Meaning |
|--------|---------|
| $V$ | **Vision confidence** — geometric mean of token probabilities on the model’s observation span ([skin-audit paper](https://qutis-health.africa/research/papers/confidence-estimation-framework)). |
| $K$ | **Audit completeness** — share of structured skin-audit checks that are filled (concern, severity, signals, derived needs). |
| $h$ | **Profile richness** — whether the product request carries stated user context (profile fields, product name, notes, etc.). |

Stated profile feeds $h$; the photo path feeds $V$ and $K$. §2 selects personalization **mode**; §4 defines how $h$, $V$, and $K$ combine into $C$ for each mode.

---

## 2. Personalization Mode and Confidence Ceilings

**Mode** $m$ fixes how strongly a product audit may use skin and profile context, and which terms enter decision confidence later. It is chosen from intake outcome and user preference, not from $D_c$ itself.

### 2.1 Choosing $m$

Priority order (first match wins):

1. **generic** — user explicitly opts out of a skin snapshot (`skin_snapshot_skipped`).
2. **personalized** — fresh skin audit with usable observations (skin snapshot + product review path).
3. **profile_only** — no fresh audit, but stated profile exists (goals, allergies, products, skin type, pregnancy, etc.) and the user checks a product.
4. **generic** — otherwise (no audit, insufficient profile).

| Mode $m$ | Typical user journey |
|----------|----------------------|
| **personalized** | Skin analysis **and** product review. |
| **profile_only** | Product review only, with profile on file. |
| **generic** | Product review only, minimal or no profile, or explicit skip of skin photo. |

### 2.2 Mode ceiling $\pi_m$

Even with perfect ingredient evidence, reported decision confidence is **capped** by mode so copy cannot sound fully personalized when the user did not take the full skin-audit path:

| Mode $m$ | Ceiling $\pi_m$ |
|----------|-----------------|
| personalized | $1.0$ |
| profile_only | $0.65$ |
| generic | $0.50$ |

After the raw blend $D_{\mathrm{raw}}$ is computed (§5),

$$
D_c = \min(D_{\mathrm{raw}},\ \pi_m).
$$

### 2.3 What each mode uses in the blend (preview)

| Mode | Vision term $V$ in $D_{\mathrm{raw}}$? | Typical role of $C$ |
|------|----------------------------------------|---------------------|
| personalized | Yes (25% weight in v1) | Built from $V$ and $K$ (§4) |
| profile_only | No separate $V$ term | Built from $h$ (§4) |
| generic | No | Fixed neutral prior (§4) |

§3 defines evidence coverage $E$; §4 defines $C$; §5 gives the full $D_{\mathrm{raw}}$ equation.

---

## 3. Evidence Coverage $E$

After the INCI list is extracted from the label (or user input), the rules engine looks up each line in the **CIR ingredient index**. **Evidence coverage** measures how much of that list is backed by retrievable safety evidence—not how severe any rule hit is, only whether we can cite indexed CIR data for each ingredient.

Let $N$ be the number of ingredients on the request and, for each position $i = 1,\ldots,N$, let $\mathrm{indexed}(i)$ be true when that line resolves to an indexed ingredient with a stable id. Then

$$
E = \begin{cases}
0 & N = 0 \\[4pt]
\dfrac{1}{N} \displaystyle\sum_{i=1}^{N} \mathrm{indexed}(i) & N \ge 1
\end{cases}
$$

with $E$ clamped to $[0, 1]$. Example: three INCI lines and two indexed → $E = 2/3$.

$E$ is the **evidence** term in decision confidence. In v1 it carries **40%** weight when $m = \texttt{personalized}$ and **55%** when $m \in \{\texttt{profile\_only}, \texttt{generic}\}$ (§5).

---

## 4. Context Score $C$

**Context score** $C \in [0, 1]$ summarizes how much **user and audit context** is available for the active mode $m$. It uses the scalars from §1 ($V$, $K$, $h$) but is not the same as any one of them: $C$ is the single number that enters the decision-confidence blend (§5).

### 4.1 Profile richness $h$ (for `profile_only`)

On the product request, set $h = 1$ if **any** of the following is non-empty: concerns, symptoms, skin type, age range, notes, escalation notes, or product name; otherwise $h = 0$.

### 4.2 Audit completeness $K$ (for `personalized`)

$K$ is the fraction of four structured audit checks that pass: primary concern, condition severity, condition signals (inflammation, barrier, or patterns), and at least one derived need. With four checks,

$$
K \in \left\{0,\ \tfrac{1}{4},\ \tfrac{1}{2},\ \tfrac{3}{4},\ 1\right\}.
$$

### 4.3 $C$ by mode

$$
C =
\begin{cases}
0.5 & m = \texttt{generic} \\[6pt]
0.55 + 0.30\,h & m = \texttt{profile\_only} \\[6pt]
0.35 & m = \texttt{personalized},\ V = 0 \text{ and } K = 0 \\[6pt]
\min\!\left(1,\ \tfrac{V + K}{2}\right) & m = \texttt{personalized},\ \text{otherwise}
\end{cases}
$$

| Mode | $C$ in words |
|------|----------------|
| **generic** | Fixed neutral prior ($0.5$); no skin audit in the blend. |
| **profile_only** | $0.55$ with sparse request context; $0.85$ when $h = 1$. |
| **personalized** | Average of vision confidence and audit completeness, or $0.35$ when both are missing. |

In **personalized** mode, $V$ also appears as its **own** term in $D_{\mathrm{raw}}$ (25% in v1), so model certainty affects decision confidence both through $C$ and through $V$ directly (§5).

---

## 5. Decision Confidence $D_c$

Decision confidence combines ingredient evidence $E$ (§3), context score $C$ (§4), and—only in **personalized** mode—vision confidence $V$ (§1). Weights depend on mode $m$ (§2). The result is capped by $\pi_m$, then mapped to guidance tone for product copy.

### 5.1 Raw blend $D_{\mathrm{raw}}$

For **personalized** mode, $V$ is taken from the skin audit when vision context is active; otherwise the vision term is zero.

$$
D_{\mathrm{raw}} =
\begin{cases}
0.40\,E + 0.35\,C + 0.25\,V & m = \texttt{personalized} \\[6pt]
0.55\,E + 0.45\,C & m \in \{\texttt{profile\_only},\ \texttt{generic}\}
\end{cases}
$$

All inputs lie in $[0, 1]$, so $D_{\mathrm{raw}} \in [0, 1]$.

**Expanded personalized form.** When $C = \tfrac{V + K}{2}$ (the usual case with audit data), substituting gives

$$
D_{\mathrm{raw}} = 0.40\,E + 0.425\,V + 0.175\,K,
$$

so the declared **25% vision** weight plus the share of $V$ inside **35% context** yields **42.5%** total influence from $V$ on $D_{\mathrm{raw}}$.

### 5.2 Final score $D_c$

Apply the mode ceiling from §2.2:

$$
D_c = \min(D_{\mathrm{raw}},\ \pi_m), \qquad D_c \in [0, 1].
$$

$D_c$ is what we log as `decision_confidence` and use for routing tone below.

### 5.3 Tier, guidance mode, and tone

$D_c$ does **not** change rules-engine safety findings; it controls **how assertive** the user-facing product report may be.

| $D_c$ | Confidence tier | Guidance mode | Tone (product report) |
|-------|-----------------|---------------|------------------------|
| $D_c \ge 0.75$ | high | **full** | Clear safety themes; concise recommendation when evidence supports it. |
| $0.50 \le D_c < 0.75$ | moderate | **cautious** | Hedged language; note evidence gaps; avoid definitive fit verdicts. |
| $D_c < 0.50$ | low | **clarify** | Short report; stress uncertainty; ask to confirm product or send a clearer label photo. |

**Personalization mode** (§2) still constrains *what* the report may say (vision vs profile vs generic disclaimers). **Guidance mode** constrains *how confidently* it may say it.

---

## 6. Personalized mode: summing the three scalars

In **personalized** mode, decision confidence is a **weighted sum of three scalars** on $[0, 1]$. Weights sum to 1: **40%** evidence, **35%** context, **25%** vision.

### 6.1 Evidence term (40%)

Let $N$ be the number of INCI lines on the product request. For each line $i = 1,\ldots,N$, let $\mathbb{1}[\mathrm{indexed}(i)]$ be 1 when that ingredient resolves in the CIR index with a stable id, and 0 otherwise (§3). Evidence coverage is

$$
E = \frac{1}{N} \sum_{i=1}^{N} \mathbb{1}[\mathrm{indexed}(i)].
$$

The **evidence contribution** to the blend is

$$
0.40 \cdot E = 0.40 \cdot \frac{1}{N} \sum_{i=1}^{N} \mathbb{1}[\mathrm{indexed}(i)].
$$

### 6.2 Vision term (25%)

For the skin observation span, let $p_1,\ldots,p_m$ be the model’s chosen-token probabilities on that span (from log-probabilities $\ell_k$ via $p_k = e^{\ell_k}$; see [skin-audit paper](https://qutis-health.africa/research/papers/confidence-estimation-framework)). **Vision confidence** is the geometric mean

$$
V = \left( \prod_{k=1}^{m} p_k \right)^{1/m}.
$$

The **vision contribution** to the blend is

$$
0.25 \cdot V = 0.25 \cdot \left( \prod_{k=1}^{m} p_k \right)^{1/m}.
$$

### 6.3 Context term (35%)

Context score $C$ is defined in §4 (personalized: typically $C = \min(1,\ \tfrac{V + K}{2})$ when audit data exists). The **context contribution** is $0.35 \cdot C$.

### 6.4 Combined sum

$$
D_{\mathrm{raw}}
= 0.40 \cdot \frac{1}{N} \sum_{i=1}^{N} \mathbb{1}[\mathrm{indexed}(i)]
+ 0.35\,C
+ 0.25 \cdot \left( \prod_{k=1}^{m} p_k \right)^{1/m}.
$$

Equivalently, using $E$ and $V$ as shorthand:

$$
D_{\mathrm{raw}} = 0.40\,E + 0.35\,C + 0.25\,V.
$$

For $m = \texttt{personalized}$, $\pi_m = 1.0$, so

$$
D_c = \min(D_{\mathrm{raw}},\ 1.0).
$$

### 6.5 Fully expanded (when $C = \tfrac{V + K}{2}$)

Substituting $C = \tfrac{1}{2}\left(\prod_{k=1}^{m} p_k\right)^{1/m} + \tfrac{K}{2}$ (and clamping where the implementation applies):

$$
\begin{aligned}
D_{\mathrm{raw}} =\;& 0.40 \cdot \frac{1}{N} \sum_{i=1}^{N} \mathbb{1}[\mathrm{indexed}(i)] \\
&+ 0.175\,K \\
&+ 0.425 \cdot \left( \prod_{k=1}^{m} p_k \right)^{1/m}.
\end{aligned}
$$

Here **0.425** on the geometric mean is **0.25** (direct vision weight) plus **0.35 × ½** (vision’s share inside context).

### 6.6 Worked example

Three INCI lines, all indexed ($N = 3$, each indicator 1): $E = 1$. Observation span with geometric mean $V = 0.80$. Audit completeness $K = 1$ → $C = 0.90$.

$$
\begin{aligned}
D_{\mathrm{raw}} &= 0.40 \cdot \frac{1}{3}(1 + 1 + 1) + 0.35(0.90) + 0.25(0.80) \\
&= 0.40 + 0.315 + 0.20 = 0.915.
\end{aligned}
$$

Same value from the expanded form: $0.40(1) + 0.175(1) + 0.425(0.80) = 0.915$. Then $D_c = 0.915$ → tier **high**, guidance **full** (§5.3).
