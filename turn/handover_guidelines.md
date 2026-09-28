# Turn.io Handover Guidelines

Guidelines for anyone (human or agent) building the **Turn.io Journey side** of Qutis.

**Golden rule:** Turn is a **relay**. The Journey handles the menu, then **ends**. Nova owns the entire client↔AI conversation via Turn's webhook. The Journey must **not** try to relay the conversation itself.

---

## Mental model

```
Journey (menu / onboarding)  →  ENDS  →  Turn webhook  →  Nova (client ↔ AI)
```

| Layer | Owns | Never does |
|-------|------|------------|
| **Journey** | Welcome, menu, buttons, consent, handoff message | Relay chat, call AI, loop on user input |
| **Turn platform** | Delivers each inbound message to the registered webhook | — |
| **Nova backend** | Full conversation, MedGemma, reports, session memory | — |

---

## What the handover IS

1. User picks **Start analysis** in the Journey.
2. The Journey sends a short handoff message.
3. The Journey **ends** (terminal card — no `then`, no `post()`).
4. Every following message is delivered by Turn to `POST /v1/webhooks/whatsapp` → Nova.
5. Nova replies via `POST https://whatsapp.turn.io/v1/messages`.

The handover is an **absence of a Journey**, not an API call. Once the Journey ends and no session is active, Turn routes inbound messages to the registered webhook automatically.

---

## What the handover is NOT (avoid these)

### ❌ Do not `post()` to the backend to "relay" the conversation

```stack
# WRONG — fires once, 20s limit, keeps the Journey active
card ActivateDermascan do
  resp = post("https://<ngrok>/v1/sessions/activate", ...)
  then(StartAnalysis)
end
```

A `post()` runs **once**. A conversation is **many messages**. One call cannot relay an ongoing chat, and while the Journey is active it **intercepts** the user's messages so they never reach Nova.

### ❌ Do not point the Journey at the webhook path

`/v1/webhooks/whatsapp` is where **Turn's platform** posts — not something the Journey calls. Posting to it from a card is both wrong-purpose and will hit auth (see below).

### ❌ Do not build a Question → post → reply loop

That recreates the conversation inside the Journey, defeating the relay and hitting the 20s HTTP ceiling.

---

## The correct handoff card

Terminal card. No `then`, no `post()`:

```stack
card StartAnalysis do
  text("""
  Great — you're all set.

  Send a clear, well-lit photo of the affected area when you're ready.
  Reply *menu* any time to return here.

  This is *not* a diagnosis — it supports self-care decisions only.
  """)
  # Journey session ends here.
  # Next inbound message → Turn whatsapp webhook → Nova.
end
```

---

## One-time setup (outside the Journey)

The relay depends on **registering Nova's webhook once** — via API, not in a Journey.

```http
PATCH https://whatsapp.turn.io/v1/settings/application
Authorization: Bearer <TURN_API_TOKEN>
Content-Type: application/json

{ "webhook_url": "https://<ngrok-host>/v1/webhooks/whatsapp" }
```

- Subscribe to the **`whatsapp`** subscription (inbound messages).
- ngrok must target **Nova (port 8088)**, not the standalone webhook server (8000).
- Correct path: `/v1/webhooks/whatsapp` (not `/v1/whatsapp`).

---

## Authentication reality

Turn signs its webhooks with **`X-Turn-Hook-Signature`** (HMAC-SHA256). Turn does **not** send Nova's `X-Internal-Secret`.

| Environment | How to reconcile |
|-------------|------------------|
| **Sandbox / dev** | Run Nova with `WHATSAPP_INGRESS_SECRET` and `INTERNAL_API_SECRET` unset so Turn can post directly. Nova logs a warning and allows ingress. |
| **Production** | Put a thin adapter in front of Nova that verifies `X-Turn-Hook-Signature`, then injects `X-Internal-Secret` before forwarding. See [`turn/relay_integration.md`](relay_integration.md). |

Sandbox launch (dev only):
```powershell
$env:WHATSAPP_INGRESS_SECRET=""; $env:INTERNAL_API_SECRET=""; poetry run uvicorn dermascan.backend.app.main:app --host 0.0.0.0 --port 8088 --reload
```

---

## Optional: routing flag (only if a platform router needs it)

If a router must distinguish Dermascan vs platform traffic, tag the contact from the Journey using the **Turn Contacts API** (still not a Nova call):

```stack
card StartAnalysis do
  patch("https://whatsapp.turn.io/v1/contacts/@contact.whatsapp_id/profile",
    timeout: 5000,
    body: """
    { "active_handler": "dermascan" }
    """,
    headers: [
      ["content-type", "application/json"],
      ["authorization", "Bearer @global.turn_api_token"]
    ]
  )
  then(HandoffMessage)
end
```

Omit this entirely if Nova receives all inbound messages directly.

---

## Return to the menu

When the user sends `menu` during a Nova session:

1. Nova detects exit intent.
2. Nova clears the flag (if used): `PATCH /v1/contacts/{wa_id}/profile` → `active_handler = null`.
3. Nova restarts the menu Journey: `POST https://whatsapp.turn.io/v1/stacks/{qutis_uuid}/start`.

Alternatively, a `CATCH ALL` / keyword trigger re-enters the menu Journey when no session is active.

---

## Author checklist

- [ ] Handoff card is terminal — no `then`, no `post()`
- [ ] No Question→post→reply loop anywhere in the Journey
- [ ] Nova webhook registered via `PATCH /v1/settings/application`
- [ ] ngrok points at Nova **8088**; path is `/v1/webhooks/whatsapp`
- [ ] `whatsapp` subscription enabled
- [ ] Auth reconciled (sandbox: secret unset · prod: adapter injects secret)
- [ ] Menu re-entry wired (`menu` → restart Journey)
- [ ] Journey menu buttons ≤ 3 (use a list for more options)

---

## Quick reference

| Direction | Endpoint | Caller |
|-----------|----------|--------|
| Inbound (relay) | `POST /v1/webhooks/whatsapp` (Nova) | **Turn platform** (auto) |
| Outbound reply | `POST https://whatsapp.turn.io/v1/messages` | Nova |
| Register webhook | `PATCH https://whatsapp.turn.io/v1/settings/application` | Ops (one-time) |
| Restart menu | `POST https://whatsapp.turn.io/v1/stacks/{uuid}/start` | Nova / platform |
| Routing flag | `PATCH https://whatsapp.turn.io/v1/contacts/{wa_id}/profile` | Journey / Nova |

See also: [`turn/relay_integration.md`](relay_integration.md) · [`turn/journeys/qutis.md`](journeys/qutis.md)
