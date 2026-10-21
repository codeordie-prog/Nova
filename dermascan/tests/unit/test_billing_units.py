"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for billing logic that needs no database."""


from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from dermascan.billing import notify, paystack_client, webhook
from dermascan.billing.db import build_conninfo

pytestmark = pytest.mark.unit


class TestAmountConversion:
    """Paystack amounts are subunits; getting this wrong charges 100x wrong."""

    def test_credits_convert_to_subunits(self) -> None:
        assert paystack_client.credits_to_minor(100) == 10000
        assert paystack_client.credits_to_minor(1) == 100

    def test_zero_credits_is_zero(self) -> None:
        assert paystack_client.credits_to_minor(0) == 0


class TestReference:
    """The reference is what ties a payment back to an account."""

    def test_reference_is_account_tagged_and_unique(self) -> None:
        account_id = "1234abcd-5678-90ef-1234-567890abcdef"
        first = paystack_client.new_reference(account_id)
        second = paystack_client.new_reference(account_id)

        assert first != second
        assert paystack_client.account_id_prefix(first) == "1234abcd"

    def test_reference_avoids_characters_paystack_rejects(self) -> None:
        reference = paystack_client.new_reference("1234abcd-5678-90ef-1234-567890abcdef")
        assert all(c.isalnum() or c in "-.=" for c in reference)

    def test_prefix_of_foreign_reference_is_none(self) -> None:
        assert paystack_client.account_id_prefix("someone-elses-ref") is None


class TestBillingEmail:
    """Paystack requires an email; a user must never be blocked for lacking one."""

    def test_real_email_is_preferred(self) -> None:
        assert paystack_client.billing_email("acct-1", "grace@example.com") == "grace@example.com"

    def test_placeholder_is_stable_per_account(self) -> None:
        first = paystack_client.billing_email("1234abcd-5678", None)
        second = paystack_client.billing_email("1234abcd-5678", None)
        assert first == second and first.endswith("@qutis.app")

    def test_malformed_email_falls_back(self) -> None:
        assert paystack_client.billing_email("acct-1", "not-an-email").endswith("@qutis.app")


class TestWebhookSignature:
    """SHA512 over the raw body. Not the SHA256 scheme used for the dashboard."""

    secret = "sk_test_units"
    body = b'{"event":"charge.success","data":{"reference":"qutis-a-1","amount":10000}}'

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.secret.encode(), body, hashlib.sha512).hexdigest()

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", self.secret)

    def test_valid_signature_passes(self) -> None:
        assert webhook.verify_signature(self.body, self._sign(self.body)) is True

    def test_tampered_body_fails(self) -> None:
        assert webhook.verify_signature(self.body + b" ", self._sign(self.body)) is False

    def test_missing_signature_fails(self) -> None:
        assert webhook.verify_signature(self.body, None) is False

    def test_reserialized_body_fails(self) -> None:
        """The trap: same data, different bytes. Parsing then re-dumping breaks it."""
        reserialized = json.dumps(json.loads(self.body)).encode()
        assert webhook.verify_signature(reserialized, self._sign(self.body)) is False

    def test_sha256_signature_is_rejected(self) -> None:
        """Guards against reusing shared/hmac_auth's scheme by mistake."""
        wrong = hmac.new(self.secret.encode(), self.body, hashlib.sha256).hexdigest()
        assert webhook.verify_signature(self.body, wrong) is False

    def test_no_key_configured_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAYSTACK_SECRET_KEY", "")
        assert webhook.verify_signature(self.body, self._sign(self.body)) is False


class TestDedupeKey:
    """Paystack legitimately re-sends webhooks."""

    def test_same_event_dedupes(self) -> None:
        payload = {"event": "charge.success", "data": {"reference": "r1", "amount": 10000}}
        assert webhook.dedupe_key(payload) == webhook.dedupe_key(dict(payload))

    def test_different_amount_is_distinct(self) -> None:
        base = {"event": "charge.success", "data": {"reference": "r1", "amount": 10000}}
        other = {"event": "charge.success", "data": {"reference": "r1", "amount": 20000}}
        assert webhook.dedupe_key(base) != webhook.dedupe_key(other)


class TestIpAllowlist:
    def test_known_paystack_ip_allowed(self) -> None:
        assert webhook.is_paystack_ip("52.31.139.75") is True

    def test_unknown_ip_rejected(self) -> None:
        assert webhook.is_paystack_ip("203.0.113.9") is False
        assert webhook.is_paystack_ip(None) is False


class TestMessageCopy:
    """Both top-up routes must always be offered."""

    def test_out_of_credits_offers_link_and_stk(self) -> None:
        text = notify.out_of_credits_message(
            authorization_url="https://checkout.paystack.com/abc", balance=0
        )
        assert "https://checkout.paystack.com/abc" in text
        assert "M-PESA number" in text

    def test_out_of_credits_without_link_still_offers_a_route(self) -> None:
        text = notify.out_of_credits_message(authorization_url=None, balance=3)
        assert "M-PESA number" in text

    def test_shortfall_is_stated_when_known(self) -> None:
        text = notify.out_of_credits_message(
            authorization_url=None, balance=5, required=20
        )
        assert "20" in text and "5" in text

    def test_confirmation_offers_to_resume_pending_work(self) -> None:
        text = notify.payment_confirmed_message(
            credits=100, balance=140, pending_task="a skin review"
        )
        assert "confirmed" in text.lower()
        assert "140" in text
        assert "a skin review" in text
        assert "?" in text

    def test_confirmation_without_pending_work_still_prompts(self) -> None:
        text = notify.payment_confirmed_message(credits=100, balance=140)
        assert "140" in text
        assert "skin review" not in text


class TestBillingDbConninfo:
    """Billing Postgres conn string includes keepalive tuning."""

    def test_build_conninfo_includes_tcp_keepalives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRESS_PASSWORD", "test-secret")
        monkeypatch.setenv("POSTGRES_HOST", "10.0.0.1")
        conninfo = build_conninfo()
        assert "keepalives=1" in conninfo
        assert "keepalives_idle=" in conninfo
