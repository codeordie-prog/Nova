# Platform Clinical Services Hub

Code-first Turn.io Journey owned by the **healthcare platform** — not Dermascan.

Routes patients to clinical services. When the user selects **Skin analysis**, the Journey activates Dermascan and **releases the session** so all further messages flow via normal WhatsApp webhooks (no 20s Journey limit).

**Deploy:** `PATCH /v1/stacks/{uuid}` with the DSL below as the `notebook` field.

**Prerequisites:**

- Contact schema: `active_handler` (STRING/ENUM), optional `dermascan_consent` (BOOLEAN)
- Turn setting: *automatically close journey chats* after completion
- Platform webhook router OR Dermascan ingress gated on `active_handler == "dermascan"`
- Dermascan `POST /v1/sessions/activate` (returns in < 1s)

---

```stack
# Hub entry — only when no external handler is active
trigger(on: "FIRST TIME")
when contact.active_handler == null or contact.active_handler == ""

trigger(on: "MESSAGE RECEIVED")
when (has_phrase(event.message.text.body, "hi")
  or has_phrase(event.message.text.body, "menu")
  or has_phrase(event.message.text.body, "hello"))
  and (contact.active_handler == null or contact.active_handler == "")

stack ClinicalServicesHub do

  card Welcome do
    text("""
    Welcome to your health service on WhatsApp.

    What can we help you with today?
    """)

    buttons([SkinAnalysis, Appointments, LabResults, MedicationSupport]) do
      text("Select a service:")
    end
  end

  # ── Skin analysis → hand off to Dermascan (external service) ─────────

  card SkinAnalysis do
    patch("@config.turn_contacts_url/@contact.whatsapp_id/profile",
      body: """
      {
        "active_handler": "dermascan",
        "dermascan_consent": true
      }
      """,
      headers: [
        ["content-type", "application/json"],
        ["authorization", "Bearer @config.turn_api_token"]
      ],
      timeout: 5_000
    )

    # Fast activation only — no photo, no AI (must return < 1s)
    activate = post("@config.dermascan_activate_url",
      body: """
      {
        "wa_id": "@contact.whatsapp_id",
        "service": "dermascan",
        "source": "platform_hub"
      }
      """,
      headers: [
        ["content-type", "application/json"],
        ["x-internal-secret", "@config.dermascan_ingress_secret"]
      ],
      timeout: 5_000
    )

    then(HandoffFailed when activate.status == null or activate.status >= 400)
  end

  card SkinAnalysis do
    text("""
    You're now connected to skin analysis.

    Send a clear photo of the affected area when you're ready.
    Reply MENU any time to return to this main menu.

    This is not a diagnosis — it supports self-care decisions only.
    """)
    # Terminal card — no `then`. Journey session ends here.
    # Next inbound message goes to whatsapp webhook → Dermascan router.
  end

  card HandoffFailed do
    patch("@config.turn_contacts_url/@contact.whatsapp_id/profile",
      body: """{"active_handler": null}""",
      headers: [
        ["content-type", "application/json"],
        ["authorization", "Bearer @config.turn_api_token"]
      ],
      timeout: 5_000
    )
    text("Sorry, we couldn't connect you right now. Please try again or reply menu.")
  end

  # ── Other platform services (stay in Journey or hand off similarly) ────

  card Appointments do
    patch("@config.turn_contacts_url/@contact.whatsapp_id/profile",
      body: """{"active_handler": "appointments"}""",
      headers: [
        ["content-type", "application/json"],
        ["authorization", "Bearer @config.turn_api_token"]
      ],
      timeout: 5_000
    )
    then(AppointmentsFlow)
  end

  card AppointmentsFlow do
    text("Appointment booking — connect your appointments Journey or service here.")
    # Or: then(EndAndRelease) after setting active_handler
  end

  card LabResults do
    text("Lab results delivery — platform-owned flow.")
    then(LabResultsFlow)
  end

  card LabResultsFlow do
    text("Connect HealthPass / secure document delivery here.")
  end

  card MedicationSupport do
    text("Medication reminders and adherence — platform-owned flow.")
    then(MedicationFlow)
  end

  card MedicationFlow do
    text("Connect medication Journey here.")
  end

end
```

---

## Message routing after handoff

Once `active_handler = "dermascan"` and the Journey ends:

```
Inbound WhatsApp message
  → Turn whatsapp webhook
  → Platform router reads contact.active_handler
  → "dermascan" : forward to Dermascan POST /v1/webhooks/whatsapp
  → Dermascan orchestrator (unlimited processing time)
  → Outbound via POST /v1/messages
```

The router can live in:

- A **platform adapter** (recommended) — single webhook, routes by contact field
- **Dermascan ingress** — rejects/ignores when `active_handler != "dermascan"` (less ideal; Dermascan shouldn't own routing logic)

## Returning to the hub

When the user sends `"menu"` during a Dermascan session:

1. Dermascan detects exit intent
2. `PATCH` contact: `active_handler = null`
3. `POST https://whatsapp.turn.io/v1/stacks/{hub_uuid}/start` with `{ "wa_id": "..." }`
4. Hub Journey greets and shows the menu again

Alternatively, a `CATCH ALL` hub trigger fires when `active_handler` is empty and the user messages.

## Configuration

| Key | Example |
|-----|---------|
| `turn_contacts_url` | `https://whatsapp.turn.io/v1/contacts` |
| `turn_api_token` | Bearer token |
| `dermascan_activate_url` | `https://nova.example.com/v1/sessions/activate` |
| `dermascan_ingress_secret` | Shared secret for activation + ingress |

## What this Journey deliberately does NOT do

- Process or forward photos (happens via webhook after session release)
- Wait for MedGemma / rules engine (no 20s timeout risk)
- Send skin analysis reports (Dermascan outbound worker)
- Own escalation logic (Dermascan → Helpdesk HMAC webhook)
