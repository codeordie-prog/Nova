# Qutis — Skincare Service (Phase 1: UI only)

Code-first Turn.io Journey for **Qutis** skincare on WhatsApp.

**Phase 1:** welcome message, menu buttons, and info stages only — **no backend HTTP calls yet**.

**Deploy:** paste the `stack` block into the Journey **Code** editor, or `PATCH /v1/stacks/{uuid}` with `notebook` + `is_canvas: false`.

---

```stack
# ── Triggers ────────────────────────────────────────────────────────────────

trigger(on: "FIRST TIME")

trigger(on: "MESSAGE RECEIVED")
when has_phrase(event.message.text.body, "hi")
  or has_phrase(event.message.text.body, "hello")
  or has_phrase(event.message.text.body, "menu")
  or has_phrase(event.message.text.body, "qutis")

# ── Journey ─────────────────────────────────────────────────────────────────

stack Qutis do

  # Main entry — welcome + menu (max 3 WhatsApp reply buttons)
  card Welcome do
    text("""
    Welcome to *Qutis* — your skincare companion on WhatsApp.

    We help you understand your skin and take better care of it.
    """)

    buttons([SkinAnalysis, HowItWorks, SkincareTips]) do
      text("What would you like to do?")
    end
  end

  # ── Skin analysis path (placeholder — backend wired in Phase 2) ───────────

  card SkinAnalysis do
    text("""
    *Skin analysis*

    You'll send a clear, well-lit photo of the affected area.
    Our system reviews it and suggests sensible next steps.

    This is *not* a diagnosis — it supports self-care decisions only.
    """)

    buttons([StartAnalysis, BackToMenu]) do
      text("Ready to continue?")
    end
  end

  # ── Handoff to Dermascan (no backend call — relies on the webhook) ────────
  #
  # We do NOT call an activate endpoint. Instead we optionally tag the contact
  # and END the session. The user's next message (their photo) is delivered by
  # Turn to Dermascan's existing webhook: POST /v1/webhooks/whatsapp.
  #
  # Optional routing flag (uncomment if the platform router reads active_handler):
  #
  #   card StartAnalysis do
  #     patch("https://whatsapp.turn.io/v1/contacts/@contact.whatsapp_id/profile",
  #       timeout: 5000,
  #       body: """
  #       { "active_handler": "dermascan" }
  #       """,
  #       headers: [
  #         ["content-type", "application/json"],
  #         ["authorization", "Bearer @global.turn_api_token"]
  #       ]
  #     )
  #     then(HandoffMessage)
  #   end

  card StartAnalysis do
    text("""
    Great — you're all set.

    Send a clear, well-lit photo of the affected area when you're ready.
    Reply *menu* any time to return here.

    This is *not* a diagnosis — it supports self-care decisions only.
    """)
    # Terminal card: no `then`. The Journey session ends here.
    # The next inbound message goes to Turn's whatsapp webhook → Dermascan.
  end

  # ── How it works ──────────────────────────────────────────────────────────

  card HowItWorks do
    text("""
    *How Qutis works*

    1. Choose *Skin analysis* from the menu
    2. Send a photo of your skin concern
    3. Receive guidance based on our analysis
    4. Reply *menu* to come back anytime

    Your data is handled securely and used only to support your care.
    """)
    then(Welcome)
  end

  # ── Skincare tips (static content for now) ────────────────────────────────

  card SkincareTips do
    text("""
    *Quick skincare tips*

    • Cleanse gently — avoid harsh scrubbing
    • Moisturise daily, especially after washing
    • Use SPF in the morning
    • Patch-test new products on a small area first

    Need a personalised check? Choose *Skin analysis* from the menu.
    """)
    then(Welcome)
  end

  # ── Return path (also used as a button target) ────────────────────────────

  card BackToMenu do
    then(Welcome)
  end

end
```

---

## Turn UI — paste this in Code view

1. Open Journey **Qutis** → click **Setup trigger** (green start box):
   - Add **First time** (new contact messages you)
   - Add **Message received** with keywords: `hi`, `hello`, `menu`, `qutis`
2. Switch from **Canvas** to **Code** (editor icon / "Code" tab in the journey builder).
3. Replace the notebook with the `stack Qutis do ... end` block above (triggers included).
4. **Save** — stays in staging until you click **Publish**.
5. Test with **Preview** (`!preview on` for your test number) or Journey Simulation API.

## Session settings (right panel)

- **Expire if inactive:** 5 minutes is fine for menu browsing; increase to **15–30 minutes** before Phase 2 photo upload.
- **Local data:** leave empty for now; we'll add variables when wiring the backend.

## Phase 2 (next)

- `SkinAnalysis` / `StartAnalysis` → `POST` Dermascan activate + release session
- Contact field e.g. `active_handler = "dermascan"`
- Return-to-hub on `menu` keyword during analysis
