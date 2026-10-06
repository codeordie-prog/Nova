# Sandbox implementation plan — Qutis 3-step Journey + webhook handoff

**Audience:** Turn.io implementer (Journey builder / API)  
**Scope:** Turn **sandbox** only — staging Journey, sandbox WhatsApp number, sandbox API token  
**Out of scope:** Production numbers, MYDAWA multi-service hub, prescription routing  

**Goal:** A **3-step** Journey (welcome → how it works → start) on the **sandbox line**. When the user taps **Start**, the Journey **ends** and all further chat is handled by the **Qutis backend** via the sandbox **`whatsapp` webhook** (normal messaging — not inside the Journey).

**Related repo docs:** `turn/handover_guidelines.md`, `turn/journeys/qutis.md`, `turn/relay_integration.md`

---

## 1. Mental model (sandbox)

```
User messages sandbox number
  → Journey (3 steps, buttons only)
  → User taps Start
  → PATCH contact active_handler = "dermascan"
  → Terminal handoff message
  → Journey session ENDS (no active Journey)

User sends text / photo
  → Turn POSTs to sandbox webhook URL (Qutis ingress)
  → Qutis orchestrator replies via POST /v1/messages (sandbox token)
```

| Layer | Sandbox responsibility |
|-------|-------------------------|
| **Turn Journey** | Welcome, “How it works”, “Start” + handoff copy only |
| **Turn platform** | Deliver inbound messages to registered webhook when Journey is not active |
| **Qutis backend** | Full conversation after handoff (agents, photos, billing tests) |

**Do not** use Journey `post()` to call the Qutis webhook or to “relay” chat. One HTTP call cannot carry a multi-turn conversation, and an active Journey blocks relay delivery.

---

## 2. Sandbox prerequisites (Turn side)

Complete these **before** publishing the Journey:

1. **Sandbox API token**  
   Turn UI → Settings → API & Webhooks → create token (sandbox).  
   Store as secret; use `Bearer` on Contacts API calls from the Journey.

2. **Custom contact profile field**  
   Create a string field on the contact schema:  
   - **Name:** `active_handler`  
   - **Purpose:** Sandbox routing flag — `"dermascan"` while Qutis owns the chat; empty/`null` when the user is in the menu Journey only.

3. **Application webhook (sandbox)**  
   Point Turn sandbox application webhook at the Qutis **sandbox ingress** (e.g. ngrok → `https://<host>/v1/webhooks/whatsapp`).  
   - Subscription: **`whatsapp`** (inbound messages + statuses).  
   - Qutis team provides the URL and verifies `X-Turn-Hook-Signature`.  
   - Respond **2xx within 5 seconds** on webhook (Qutis enqueues async work).

4. **Journey setting**  
   Enable **automatically close journey chats** when non-operator Journeys complete (so the handoff card releases the session).

5. **Optional sandbox router**  
   If the sandbox webhook hits a **small adapter** instead of Qutis directly, forward to Qutis only when `contact.active_handler == "dermascan"`.  
   For a **dedicated sandbox number used only for Qutis tests**, webhook may point straight to Qutis; still set `active_handler` at handoff so return-to-menu behaviour can be tested later.

---

## 3. Journey design — exactly 3 user-visible steps

| Step | Card | User sees |
|------|------|-----------|
| **1** | `Welcome` | Welcome text + **2 reply buttons:** “How it works”, “Start” |
| **2** | `HowItWorks` | Short explanation + button **Back** (returns to Welcome) |
| **3** | `StartHandoff` | PATCH `active_handler`, handoff text, **Journey ends** (no `then`) |

WhatsApp allows **max 3 reply buttons** per message; step 1 uses 2 so “Start” can also be reached from Welcome for fewer taps.

**Triggers (sandbox):**

- `FIRST TIME` — new contact messages the sandbox number.  
- `MESSAGE RECEIVED` when body contains `hi`, `hello`, `menu`, or `qutis` **and** `active_handler` is empty (so users can reopen the menu after Qutis clears the flag).

---

## 4. Journey notebook (code-first)

Deploy via Journey **Code** editor or `PATCH /v1/stacks/{uuid}` with `notebook` + publish when ready.

**Configuration keys** (set in Journey config / globals — do not hardcode secrets in the notebook):

| Key | Example | Use |
|-----|---------|-----|
| `turn_api_token` | Sandbox Bearer token | Contacts PATCH |
| `turn_contacts_url` | `https://whatsapp.turn.io/v1/contacts` | Profile updates |

```stack
# ── Triggers (sandbox) ─────────────────────────────────────────────────────

trigger(on: "FIRST TIME")
when contact.active_handler == null or contact.active_handler == ""

trigger(on: "MESSAGE RECEIVED")
when (has_phrase(event.message.text.body, "hi")
  or has_phrase(event.message.text.body, "hello")
  or has_phrase(event.message.text.body, "menu")
  or has_phrase(event.message.text.body, "qutis"))
  and (contact.active_handler == null or contact.active_handler == "")

# ── Stack: Qutis Sandbox Onboarding (3 steps) ─────────────────────────────

stack QutisSandbox do

  # Step 1 — Welcome + entry buttons
  card Welcome do
    text("""
    Welcome to *Qutis* on WhatsApp (sandbox).

    We help with skincare decisions — skin snapshots and product checks.
    This is informational guidance, not a medical diagnosis.
    """)

    buttons([HowItWorks, StartHandoff]) do
      text("What would you like to do?")
    end
  end

  # Step 2 — How it works (informational)
  card HowItWorks do
    text("""
    *How it works*

    1. Tap *Start* when you're ready
    2. Chat with Qutis — send text or a clear skin photo
    3. Get guidance based on our analysis
    4. Reply *menu* anytime to return to this welcome flow (sandbox)

    Your messages after Start are handled by the Qutis service, not this menu.
    """)
    buttons([Welcome]) do
      text("Back")
    end
  end

  # Step 3 — Start: set routing flag + handoff (terminal)
  card StartHandoff do
    patch("@config.turn_contacts_url/@contact.whatsapp_id/profile",
      body: """
      {
        "active_handler": "dermascan"
      }
      """,
      headers: [
        ["content-type", "application/json"],
        ["authorization", "Bearer @config.turn_api_token"]
      ],
      timeout: 5_000
    )

    text("""
    You're connected to *Qutis* (sandbox).

    Send a message or a clear, well-lit skin photo when you're ready.
    Reply *menu* to return to this welcome screen.

    This is not a diagnosis — it supports self-care decisions only.
    """)
    # Terminal card: no `then`. Journey session must end here.
    # Next inbound message → whatsapp webhook → Qutis backend.
  end

end
```

### Handoff rules (mandatory)

- **Do** PATCH `active_handler` to `"dermascan"` on `StartHandoff` before the handoff text (or in a preceding card merged into step 3 if PATCH must be isolated — keep total user-visible steps at 3).  
- **Do not** add `then(...)` after the handoff text on `StartHandoff`.  
- **Do not** `post()` to Qutis webhook URLs from the Journey.  
- **Do not** keep the user in a Question/`ask()` loop after Start.

If PATCH fails (timeout / 4xx), add a optional failure card (sandbox QA): clear handler, show “Could not connect — try again or reply menu.”

---

## 5. Turn UI checklist (sandbox)

1. Create Journey **Qutis Sandbox** (code-first).  
2. Paste triggers + `stack QutisSandbox` block in **Code** view.  
3. Wire **config** for `turn_api_token` and `turn_contacts_url` (Turn secrets / journey config — not in source control).  
4. **Setup trigger** in UI to match section 3 (First time + keywords).  
5. Session: **Expire if inactive** — 5–15 minutes is enough for menu; Qutis owns timing after handoff.  
6. **Save** in staging; test with **Preview** / sandbox test number (`!preview on` if used).  
7. **Publish** to sandbox when simulation passes.

---

## 6. Sandbox webhook + Qutis (coordination)

**Turn implementer:**

- Confirm sandbox `webhook_url` and `whatsapp` subscription with Qutis team.  
- Confirm webhook secret for signature verification is shared securely.

**Qutis team (sandbox):**

- Run ingress (e.g. uvicorn) reachable from Turn sandbox (ngrok or similar).  
- Path: `POST /v1/webhooks/whatsapp`.  
- Outbound: `POST https://whatsapp.turn.io/v1/messages` with **sandbox** Bearer token.  
- For dev, ingress secret may be unset so Turn can post directly; production-style adapter is not required for first sandbox test.

After handoff, **every** user message should appear in Qutis logs as a normal WhatsApp ingress job until `active_handler` is cleared.

---

## 7. Return to welcome (sandbox behaviour)

When the user sends **`menu`** (or agreed keywords) **during** a Qutis session:

1. Qutis (or sandbox adapter) `PATCH`es contact profile: `active_handler = null`.  
2. Qutis or Turn operator calls `POST https://whatsapp.turn.io/v1/stacks/{this_journey_uuid}/start` with `{ "wa_id": "<user whatsapp id>" }` to re-enter **Welcome**.  
   - Journey UUID is the sandbox **Qutis Sandbox** stack id.  
   - Cannot start if user still has an **active Journey session** — handoff must have ended.

Document the sandbox Journey UUID in Turn once created.

---

## 8. Sandbox test plan

| # | Action | Expected |
|---|--------|----------|
| 1 | New user sends `hi` to sandbox number | Journey: Welcome + 2 buttons |
| 2 | Tap **How it works** | Step 2 text + Back |
| 3 | Back → Welcome | Step 1 again |
| 4 | Tap **Start** | Handoff text; Journey session closed |
| 5 | Send text “test” | Qutis webhook receives; bot replies via Turn API |
| 6 | Send image | Qutis ingress + orchestrator (skin path) |
| 7 | Send `menu` (after Qutis implements exit) | `active_handler` cleared; Journey Welcome restarts |

**Simulation API (optional):** `POST /v1/journeys/{uuid}/simulation` for steps 1–3 without live WhatsApp.

---

## 9. Deliverables for Turn implementer

- [ ] Contact field `active_handler` on sandbox org  
- [ ] Journey **Qutis Sandbox** notebook (section 4) in **staging**, then published to sandbox  
- [ ] Triggers: first time + keywords when handler empty  
- [ ] Setting: auto-close journey chats on completion  
- [ ] Sandbox webhook URL + `whatsapp` subscription confirmed with Qutis  
- [ ] Sandbox Journey UUID shared with Qutis (for `menu` restart)  
- [ ] Short test report against section 8  

---

## 10. References (Turn docs)

- Journeys overview: https://whatsapp.turn.io/docs/journeys/journeys_overview  
- HTTP in Journeys (`patch`, `post` limits): https://whatsapp.turn.io/docs/journeys/api_integrations  
- Start journey API: https://whatsapp.turn.io/docs/api/stacks  
- Webhooks: https://whatsapp.turn.io/docs/api/webhooks  

---

*Document version: sandbox plan v1 — Qutis 3-step handoff.*
