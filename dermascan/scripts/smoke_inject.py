"""Smoke-test helper: inject one low-confidence case onto escalation:jobs.

Mimics the producer so the real escalation worker enriches, relays to the dashboard,
and populates the Redis cache the return webhook + image proxy depend on. No MedGemma needed.

    poetry run python dermascan/scripts/smoke_inject.py
    SMOKE_IMAGE_BLOB=client-media/<id>/<file>.jpg poetry run python dermascan/scripts/smoke_inject.py
"""

import json
import os
import time

from dotenv import load_dotenv
import redis

load_dotenv()

_redis = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

CASE_ID = os.environ.get("SMOKE_CASE_ID", f"smoke-{int(time.time())}")
CLIENT_ID = os.environ.get("SMOKE_CLIENT_ID", "254712345678")  # WhatsApp-style id

# Seed one conversation turn so the handoff carries context (log is newest-first; worker reverses)
_redis.lpush(
    f"orchestrator:conversation_log:{CLIENT_ID}",
    json.dumps(
        {"ts_ms": int(time.time() * 1000), "role": "user", "text": "Please check this rash on my cheek"}
    ),
)

fields = {
    "request_id": CASE_ID,
    "case_id": CASE_ID,
    "client_id": CLIENT_ID,
    "message_id": "wamid.SMOKE",
    "channel": "whatsapp",
    "source_agent": "medgemma",
    "mode": "diagnose",
    "reason": "low_confidence",
    "confidence": "0.42",
    "threshold": "0.75",
    "requires_human_review": "true",
    "diagnosis_text": "Acne vulgaris, moderate (SMOKE TEST)",
    "product_name": "",
    "description": "",
    "indications": "",
    "safety_in_pregnancy": "",
    "contraindications": "",
    "how_to_use": "",
    "special_instructions": "",
    # Point at a real client-media/... blob to get a 200 image; blank => 404 (still proves HMAC).
    "image_blob_path": os.environ.get("SMOKE_IMAGE_BLOB", ""),
    "review_type": "skin review",
    "rationale": "Low confidence; routing to human review.",
    "summary": json.dumps({"severity": "moderate", "active_inflammation": "yes"}),
    "treatment": json.dumps({"needs": {"barrier_repair": "yes"}, "contraindicated_approaches": []}),
}

stream = os.environ.get("ESCALATION_STREAM", "escalation:jobs")
msg_id = _redis.xadd(stream, fields)
print(f"injected case_id={CASE_ID} msg_id={msg_id} stream={stream}")
print(f"export CASE_ID={CASE_ID}")
