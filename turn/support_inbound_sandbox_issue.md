# Turn.io Integration Issue Report — Qutis Health / Dermascan

**Document for:** Turn.io Support and Platform Developers  
**Date:** September 2026  
**Contact:** Kelvin Ndeti · kndeti@mydawa.com · support@qurtis.com · Mydawa | Qutis Health

**Subject line for email:** Shared sandbox inbound not attributed — `qutis-3142` (account `number:8801`) + webhook review on sandbox and production numbers

---

## 1. Purpose

We are integrating an **external API service for AI skin care analysis** hosted at **[https://qutis-health.africa](https://qutis-health.africa)** with **Turn.io WhatsApp**.

Turn.io is the **WhatsApp transport layer** (Journeys for menu/onboarding, webhooks for conversation relay). Our backend (**Dermascan / Nova**) owns the clinical AI conversation after handoff.

We are **blocked** because **inbound WhatsApp messages from our test phone never reach Turn** on the shared sandbox — even though outbound API messaging and webhook delivery for outbound echoes work.

We need Turn.io to:

1. **Investigate sandbox join / inbound routing** on the shared number.
2. **Review webhook configuration** on **both** the **sandbox (`qutis-3142`)** and **production WhatsApp numbers** in our organisation — we must not route production traffic incorrectly while fixing the sandbox.

---

## 2. Integration architecture (target state)

```
WhatsApp user
    -> Turn.io (Journeys: menu, onboarding)
    -> Handoff (Journey ends)
    -> Turn webhook (whatsapp subscription)
    -> External API: qutis-health.africa / Dermascan backend
    -> Turn API POST /v1/messages (replies)
```

**References:** [Turn.io Webhooks API](https://whatsapp.turn.io/docs/api/webhooks) · [Turn.io Settings API](https://whatsapp.turn.io/docs/api/settings)

---

## 3. Environment under test

| Item | Value |
|------|--------|
| External service | [https://qutis-health.africa](https://qutis-health.africa) (skin care AI API) |
| Sandbox entry code | `qutis-3142` |
| API token account (JWT `sub`) | `number:8801` |
| Test phone (wa_id) | `254765455121` |
| Shared sandbox WhatsApp line | `+27 60 013 6875` |
| Webhook URL (dev tunnel) | `https://negative-many-feminist.ngrok-free.dev/v1/webhooks/whatsapp` |
| UI webhook subscriptions | `whatsapp`, `turn`, `journey_outbound` (all enabled) |
| Primary application webhook | Same URL via `PATCH /v1/settings/application` |

**Production numbers:** We also have production WhatsApp line(s) on this Turn organisation. Please confirm webhook settings on **production** are separate from sandbox and are not affected by our sandbox testing.

---

## 4. Sandbox join procedure (shared number)

Turn documentation states users must **join** a sandbox on the shared line before messages are attributed to that sandbox.

**Expected flow:**

1. User opens WhatsApp chat with **+27 60 013 6875** (shared sandbox number).
2. User sends the **entry code** as a message: `qutis-3142` (some docs describe this as “join {code}”).
3. Turn associates the user’s phone with sandbox **`qutis-3142`**.
4. Further messages from that phone are handled by that sandbox (inbox, Journeys, webhooks).

**What we observe:**

| Step | Expected | Actual |
|------|----------|--------|
| Send `qutis-3142` from `254765455121` | Sandbox join / auto-reply | **No consistent auto-reply**; message not in sandbox inbox |
| Send `hello` after join | In Turn inbox; `whatsapp` webhook fires | **Not in inbox**; **no `whatsapp` webhook** |
| WhatsApp delivery ticks | — | Sometimes **double ticks** (delivered to shared number) |
| Turn records inbound | `first_message_received_at` set | **Always null** on contact profile API |

**Conclusion:** Messages may reach the **shared WhatsApp number** at Meta level but are **not attributed to sandbox `qutis-3142`** on Turn. This matches known limitations of the shared sandbox (People/inbox features unreliable on shared numbers per Turn Learn docs).

---

## 5. Webhook configuration — please verify on BOTH sandbox and production

We request Turn.io to **audit webhook setup** in two places:

### 5.1 Sandbox (`qutis-3142`, account `number:8801`)

| Check | Our state | Please confirm |
|-------|-----------|----------------|
| UI webhook URL | `.../v1/webhooks/whatsapp` (full path) | Correct for our backend |
| Subscriptions | `whatsapp` enabled (required for inbound) | Inbound contact messages |
| Primary webhook (`PATCH /v1/settings/application`) | Same URL | Matches UI webhook |
| Duplicate / root-only URLs | Removed (was causing 404 noise) | None remaining |
| HMAC secret | Configured; signatures verify | OK |

### 5.2 Production WhatsApp number(s)

| Check | Request |
|-------|---------|
| Webhook URLs | Confirm production webhooks point to **production** endpoints, not our dev ngrok URL |
| Subscriptions | Confirm `whatsapp` (and any other needed) without cross-routing sandbox traffic |
| Primary webhook | Confirm per-number isolation — sandbox token must not alter production webhooks |
| Impact of our testing | Confirm no production webhook was overwritten during sandbox API token use |

**Why this matters:** We integrate a **live clinical API** ([qutis-health.africa](https://qutis-health.africa)). Misconfigured production webhooks could affect real patients. We need explicit confirmation that **sandbox and production webhook configs are isolated**.

---

## 6. What works (proven with logs)

1. **Outbound API:** `POST https://whatsapp.turn.io/v1/messages` returns **200**. Messages deliver to `254765455121` from the sandbox line.

2. **Webhook delivery (Turn -> our endpoint):** Turn POSTs to our URL; **200 OK** in Turn webhook UI logs and tunnel logs.

3. **HMAC verification:** `X-Turn-Hook-Signature` validates successfully against our webhook secret.

4. **`turn` subscription:** After our outbound API sends, Turn POSTs **`X-Turn-Hook-Subscription: turn`** (outbound echo) from IP **`34.76.11.105`**. These arrive reliably.

5. **Minimal echo server:** We deployed a handler that follows Turn webhook docs only (no full backend). On **simulated** `whatsapp` payloads, inbound is received and a reply is sent via `POST /v1/messages` — full loop works.

6. **Webhook UI logging:** Turn UI now shows successful webhook deliveries (previously empty). Responses show `ignored` for `turn` echoes (expected) until real inbound arrives.

---

## 7. What does not work

1. **Inbound from test phone never appears in Turn UI inbox** for `qutis-3142`.

2. **Contact profile API shows no inbound:**
   - `GET /v1/contacts/254765455121/profile`
   - `first_message_received_at`: **null**
   - `last_message_received_at`: **null**

3. **Inbox API:** Only **outbound** messages (sent by our API tests). **Zero inbound** from the user.

4. **No `whatsapp` subscription webhook for user replies.** We never receive `X-Turn-Hook-Subscription: whatsapp` with the user’s message body. Only `turn` (our own outbound echoes).

5. **Sandbox join (`qutis-3142`)** does not reliably connect the test phone to this sandbox account.

---

## 8. Webhook behaviour summary

| Event | `X-Turn-Hook-Subscription` | Received? | Notes |
|-------|---------------------------|-----------|--------|
| Our API outbound send | `turn` | Yes | Echo of message we sent |
| User reply `hello` on WhatsApp | `whatsapp` | **No** | **Blocking issue** |
| Simulated inbound (local test) | `whatsapp` | Yes | Echo reply sent successfully |

**Sample Turn inbound IP:** `34.76.11.105` (webhook POSTs confirmed)

---

## 9. Steps already taken (troubleshooting)

- Sent entry code **`qutis-3142`** from **`254765455121`**, then **`hello`**
- Replied within **24-hour window** to outbound API messages
- Registered webhook in **Turn UI** and via **`PATCH /v1/settings/application`**
- Used correct path **`/v1/webhooks/whatsapp`** (not root `/`)
- Removed duplicate root-only webhook URL
- Confirmed **`whatsapp`** subscription enabled on UI webhook
- Tested with **VPN disabled** on PC and phone
- Verified tunnel and endpoint health (`GET /health` -> 200)
- Replaced full backend with **minimal Turn-doc-compliant echo server** to eliminate our application as cause

---

## 10. Requests to Turn.io

Please help us with the following:

1. **Sandbox join:** Is phone **`254765455121`** correctly joined to sandbox **`qutis-3142`** on **`+27 60 013 6875`**? If not, what is required to fix attribution?

2. **Inbound routing:** Why do WhatsApp messages show delivered on the client but **never appear in Turn inbox** and **never fire `whatsapp` webhooks**?

3. **Shared sandbox limits:** Is inbound on the shared sandbox line known to be broken or limited for our org? Should we **create a new sandbox** or move to a **dedicated number**?

4. **Webhook audit (sandbox):** Confirm UI webhook + primary webhook + subscriptions are correct for **`qutis-3142`**.

5. **Webhook audit (production):** Confirm **production number(s)** webhooks are correct, isolated from sandbox, and suitable before we connect [qutis-health.africa](https://qutis-health.africa) in production.

---

## 11. Impact

We cannot validate the **Turn -> external API -> Turn** relay for skin care conversations until Turn **records and webhooks inbound contact messages** on the `whatsapp` subscription.

Outbound-only testing is insufficient for a **two-way clinical chat** product.

---

## 12. Contact

**Name:** Kelvin Ndeti  
**Organisation:** Mydawa | Qutis Health  
**Email:** kndeti@mydawa.com · support@qurtis.com  
**Service:** [https://qutis-health.africa](https://qutis-health.africa)  
**Turn sandbox:** `qutis-3142` · account `number:8801`

Thank you for your assistance.
