# Turn.io Sandbox Inbound — Retest Confirmation (Resolved)

**Document for:** Turn.io Support and Platform Developers  
**Date:** 22 September 2026  
**Organisation:** Mydawa | Qutis Health  
**Contact:** Kelvin Ndeti · kndeti@mydawa.com · support@qurtis.com  
**External service:** [https://qutis-health.africa](https://qutis-health.africa) (AI skin care API integration)

**Related prior report:** `support_inbound_sandbox_issue.pdf` (inbound not attributed on shared sandbox)

---

## 1. Summary

Following work by the Turn.io team, we **retested** the shared sandbox **`qutis-3142`** (account **`number:8801`**) and confirm that **inbound WhatsApp messages now reach Turn and are delivered to our webhook** on the **`whatsapp` subscription**.

**Outbound API messaging had already worked.** The previous blocker was **inbound contact messages** not appearing in Turn and not firing `whatsapp` webhooks. **That path is now working** in our environment.

Thank you for the fix.

---

## 2. Test environment

| Item | Value |
|------|--------|
| Sandbox entry code | `qutis-3142` |
| API token account (JWT `sub`) | `number:8801` |
| Test phone (wa_id) | `254765455121` |
| Shared sandbox line | `+27 60 013 6875` |
| Webhook URL (dev) | `https://negative-many-feminist.ngrok-free.dev/v1/webhooks/whatsapp` |
| Webhook subscriptions | `whatsapp`, `turn`, `journey_outbound` (UI) |
| Primary application webhook | Same URL (`PATCH /v1/settings/application`) |
| Test handler | Minimal echo server per [Turn Webhooks API](https://whatsapp.turn.io/docs/api/webhooks) |
| Local port / tunnel | Echo server **8088**, ngrok **8088** |

We used a **minimal echo server** (not our full production backend) to isolate Turn relay behaviour: on inbound `messages[]`, reply via `POST /v1/messages`.

---

## 3. What we observed (successful retest)

### 3.1 Inbound from WhatsApp user

Turn POSTed to our webhook with:

- **Header:** `X-Turn-Hook-Subscription: whatsapp`
- **Source IP:** `34.76.11.105`
- **HTTP:** `200 OK`
- **HMAC:** `X-Turn-Hook-Signature` **valid** (`sig=valid` in our logs)
- **Payload:** Top-level `contacts` + `messages[]` (Turn flat inbound shape)
- **Sender:** `from=254765455121`
- **Sample inbound texts:** `hello`, `join qutis-3142`

Example log lines (echo server, 22 Sep 2026):

```
WEBHOOK sub=whatsapp sig=valid keys=['contacts', 'messages']
INBOUND from=254765455121 type=text text='hello'
POST /v1/webhooks/whatsapp HTTP/1.1 200 OK  (34.76.11.105)
```

### 3.2 Outbound echo reply

After each inbound `whatsapp` event, our handler called:

`POST https://whatsapp.turn.io/v1/messages` → **200 OK**

User received WhatsApp text: echo confirmation that the relay works.

### 3.3 Expected `turn` subscription follow-up

After our API outbound send, Turn POSTed **`X-Turn-Hook-Subscription: turn`** (outbound echo). This behaved as documented and **did not block** inbound testing.

### 3.4 ngrok request log (summary)

| Time (UTC+3) | Subscription | IP | Result |
|--------------|--------------|-----|--------|
| 08:57:54 | `whatsapp` | 34.76.11.105 | 200 OK |
| 08:58:08 | `whatsapp` | 34.76.11.105 | 200 OK |
| 08:58:54 | `whatsapp` | 34.76.11.105 | 200 OK |
| (after each) | `turn` | 34.76.11.105 | 200 OK (echo) |

---

## 4. Contrast with prior failure (for your records)

| Check | Before Turn fix | After retest |
|-------|-----------------|--------------|
| User message in Turn UI inbox | Not visible | **Working** (user confirmed messages flowing) |
| `whatsapp` webhook for user reply | Never received | **Received** |
| `first_message_received_at` (API) | `null` | Not re-checked in this note; user flow confirmed in UI + webhooks |
| Only `turn` (API outbound echo) | Yes | Still present; **plus** real `whatsapp` inbound |
| WhatsApp double ticks without Turn | Observed | **Resolved** for our test phone on `qutis-3142` |

---

## 5. Sandbox join note

During retest the user sent **`join qutis-3142`** (and **`hello`**). Inbound for both was webhooked successfully. We continue to recommend documenting the exact join phrase (`qutis-3142` vs `join qutis-3142`) in sandbox onboarding materials to reduce confusion.

---

## 6. Confirmation statement

We confirm that, as of **22 September 2026**, on sandbox **`qutis-3142`** / account **`number:8801`**:

1. **Inbound WhatsApp messages** from **`254765455121`** are **received by Turn** and **forwarded to our webhook** on the **`whatsapp` subscription**.
2. **Webhook delivery** to our HTTPS endpoint via ngrok is **reliable** (200 OK, valid signature).
3. **Outbound replies** via **`POST /v1/messages`** complete the **round trip** to the user on WhatsApp.

This unblocks our integration of [qutis-health.africa](https://qutis-health.africa) with Turn.io as the WhatsApp transport layer.

---

## 7. Next steps on our side (not a Turn action item)

- Replace echo server with **Dermascan / Nova** ingress on the same webhook path.
- Run orchestrator workers for AI conversation (beyond echo).
- Configure **Turn outbound** for production (`qutis-health.africa` / staging URLs).
- Keep **sandbox and production webhooks isolated** as previously requested.

---

## 8. Contact

**Kelvin Ndeti**  
Mydawa | Qutis Health  
kndeti@mydawa.com · support@qurtis.com  

Thank you again for resolving the shared sandbox inbound issue.
