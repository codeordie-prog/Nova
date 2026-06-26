# Observability with LangSmith

A simple, elegant plan for tracing every agent turn in the Dermascan system
(orchestrator, diagnose, knowledge, outbound sender, group report aggregator)
using **LangSmith**.

LangSmith is the native tracing backend for LangChain `create_agent`. Because the
whole system is built on LangChain agents, tracing is mostly a matter of setting a
few environment variables, no code rewrites required.

---

## What we get

- **End-to-end traces** of each turn: prompt, model calls, tool calls, fallbacks, latency, token usage.
- **Per-worker projects** so orchestrator vs. diagnose vs. knowledge traces stay separate.
- **Rich metadata** (`client_id`, `request_id`, `case_id`, `channel`, `flow`) to filter and debug a single user's journey.
- A natural complement to the existing `agent_call_tracer.py` (local JSON lines) without replacing it.

---

## Step 1: Create a LangSmith account

1. Go to <https://smith.langchain.com> and sign up (GitHub/Google or email).
2. Open **Settings -> API Keys** and click **Create API Key**.
3. Copy the key (`lsv2_...`). Treat it like a secret, never commit it.

## Step 2: Add the dependency

LangSmith ships with LangChain, but pin it explicitly so traces never silently break:

```bash
poetry add langsmith
```

## Step 3: Configure environment variables

Add these to your `.env` (and `.env_example` with placeholder values). These are the
only switches needed to turn tracing on globally.

```dotenv
# --- LangSmith observability ---
LANGSMITH_TRACING=true
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_API_KEY=lsv2_your_key_here
LANGSMITH_PROJECT=dermascan-dev
```

Notes:
- Set `LANGSMITH_TRACING=false` to disable tracing instantly (e.g. in CI or load tests).
- Use distinct `LANGSMITH_PROJECT` values per environment: `dermascan-dev`, `dermascan-staging`, `dermascan-prod`.

## Step 4: Verify it works (zero code)

Because every worker already builds agents via LangChain `create_agent`, just run
any worker and trigger one message. Traces appear automatically under your project
in the LangSmith UI.

```bash
poetry run python -m dermascan.backend.services.agents.orchestrator_worker
```

Open <https://smith.langchain.com> -> your project -> you should see a trace tree
with the model chain (`openai:gpt-4o -> gemini`) and tool calls.

---

## Step 5: Separate projects per worker (recommended)

Give each worker its own project so traces don't mix. Set the project name once at
the top of each worker module, before agents are built:

```python
import os
os.environ.setdefault("LANGSMITH_PROJECT", "dermascan-orchestrator")
```

Suggested names:

| Worker | Project |
| --- | --- |
| `orchestrator_worker` | `dermascan-orchestrator` |
| `diagnose_worker` | `dermascan-diagnose` |
| `knowledge_worker` | `dermascan-knowledge` |
| `outbound_sender_worker` | `dermascan-outbound` |
| `group_report_aggregator` | `dermascan-reports` |

## Step 6: Attach metadata and tags to each turn

This is what makes traces searchable. Pass `metadata` and `tags` through the
`RunnableConfig` already used when invoking agents (the same `config` that carries
the checkpointer `thread_id`):

```python
config: RunnableConfig = {
    "configurable": {"thread_id": client_id},
    "run_name": f"orchestrator:{flow}",
    "tags": [channel, flow],
    "metadata": {
        "client_id": client_id,
        "request_id": request_id,
        "case_id": case_id,
        "channel": channel,
        "flow": flow,
    },
}
result = agent.invoke({"messages": [...]}, config=config)
```

In the LangSmith UI you can then filter by `metadata.client_id` to replay a single
user's full conversation, or by `tags` to inspect all `whatsapp` failures.

## Step 7: Watch for fallbacks and errors

The orchestrator and knowledge agents fall back `openai -> gemini` via
`wrap_model_call`. In LangSmith each provider attempt is a child run, so a trace
showing two model children = a fallback happened. To make this obvious, set
`run_name` inside the fallback middleware or rely on the existing
`agent_call_tracer.record_model_attempt` JSON lines for aggregate counts.

Create a **Monitor** in LangSmith (project -> Monitors) for:
- **Error rate** per project.
- **P50/P99 latency** per turn.
- **Token usage / cost** trends.

---

## Step 8: Keep secrets and PII safe

- Never log raw API keys; only the LangSmith key lives in `.env`.
- Skin images and personal text are sensitive. Use
  `LANGSMITH_HIDE_INPUTS=true` / `LANGSMITH_HIDE_OUTPUTS=true` in production if you
  must avoid storing prompt/response bodies, keeping only structure, timing, and metadata.

---

## How this fits the existing tracer

`agent_call_tracer.py` gives a lightweight, local, per-turn JSON summary
(provider attempts, fallback count, tool names, duration). LangSmith gives the full
remote trace tree. Keep both:

- **`agent_call_tracer`** -> cheap, always-on, offline log analysis.
- **LangSmith** -> deep interactive debugging, monitoring, and cost tracking.

Together they cover local forensics and live observability with almost no extra code.

---

## Pipeline / stream tracing (LangSmith)

Redis stream hops (produce → consume → ack) are recorded as LangSmith **chain** runs in
the **`dermascan-pipeline`** project via `dermascan/shared/stream_pipeline_tracing.py`.

### What is traced

| Event | Run name pattern | Where |
| --- | --- | --- |
| Produce | `stream:produce:<stream>` | Ingress API, orchestrator outbound, MedGemma gateway, escalation |
| Consume | `stream:consume:<stream>` | Orchestrator readers, MedGemma worker, outbound sender |

### Correlation

Every span includes `request_id`, `case_id`, `client_id`, `message_id`, `channel`,
`stream`, and `msg_id` in metadata when present on the Redis message.

When a produce happens inside an active LangSmith trace, `trace_run_id` is attached to
the Redis fields so downstream consumers can link as child runs.

### Environment

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PIPELINE_TRACING=true
LANGSMITH_PIPELINE_PROJECT=dermascan-pipeline
```

Set `LANGSMITH_PIPELINE_TRACING=false` to disable pipeline spans while keeping agent traces.

### Filtering in LangSmith

- Project: `dermascan-pipeline`
- Metadata: `metadata.request_id`, `metadata.stream`, `metadata.direction` (`produce` / `consume`)
- Tags: channel values when present on the message
