"""
End-to-end billing: a payment lands, is verified, and becomes credits.

Covers the failures that cost real money — replayed webhooks, forged amounts,
a webhook that never arrives — rather than only the happy path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

SECRET = "sk_test_integration"


def _verified(amount: int = 10000, currency: str = "KES") -> dict[str, Any]:
    """Build a Paystack verification payload."""
    return {
        "status": "success",
        "amount": amount,
        "currency": currency,
        "channel": "mobile_money",
        "paid_at": "2026-08-20T12:00:00Z",
        "customer": {"email": "grace@example.com"},
    }


@pytest.fixture
def billing_env(billing_db, billing_redis, monkeypatch: pytest.MonkeyPatch):
    """Billing enabled, with test keys and an isolated database and cache."""
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", SECRET)
    monkeypatch.setenv("PAYSTACK_CALLBACK_URL", "https://example.test/billing/return")
    return billing_redis


class TestAccountLifecycle:
    def test_new_user_gets_the_free_trial_exactly_once(self, billing_env) -> None:
        from dermascan.billing import accounts

        first = accounts.resolve_for_client("telegram", "tg-1")
        second = accounts.resolve_for_client("telegram", "tg-1")

        assert first.balance_credits == 40
        assert second.balance_credits == 40
        assert first.id == second.id

    def test_separate_channels_are_separate_accounts(self, billing_env) -> None:
        from dermascan.billing import accounts

        telegram = accounts.resolve_for_client("telegram", "same-id")
        whatsapp = accounts.resolve_for_client("whatsapp", "same-id")
        assert telegram.id != whatsapp.id


class TestFulfilment:
    def _intent(self, account_id: str, credits: int = 100) -> str:
        from dermascan.billing import intents, paystack_client

        reference = paystack_client.new_reference(account_id)
        intents.create(
            account_id=account_id,
            reference=reference,
            amount_minor=paystack_client.credits_to_minor(credits),
            currency="KES",
            credits_to_grant=credits,
            method=intents.METHOD_STK,
            msisdn="+254722000000",
        )
        return reference

    def test_verified_payment_grants_credits(self, billing_env) -> None:
        from dermascan.billing import accounts, fulfilment

        account = accounts.resolve_for_client("telegram", "tg-pay-1")
        reference = self._intent(account.id)

        with patch("dermascan.billing.paystack_client.verify", return_value=_verified()):
            result = fulfilment.fulfil(reference)

        assert result.granted
        assert result.credits_granted == 100
        assert result.balance_after == 140
        assert accounts.get_by_id(account.id).balance_credits == 140

    def test_replayed_webhook_does_not_double_credit(self, billing_env) -> None:
        from dermascan.billing import accounts, fulfilment

        account = accounts.resolve_for_client("telegram", "tg-pay-2")
        reference = self._intent(account.id)

        with patch("dermascan.billing.paystack_client.verify", return_value=_verified()):
            fulfilment.fulfil(reference)
            replay = fulfilment.fulfil(reference)

        assert replay.credits_granted == 0
        assert accounts.get_by_id(account.id).balance_credits == 140

    def test_amount_mismatch_is_refused(self, billing_env) -> None:
        from dermascan.billing import accounts, fulfilment

        account = accounts.resolve_for_client("telegram", "tg-pay-3")
        reference = self._intent(account.id)

        with patch(
            "dermascan.billing.paystack_client.verify", return_value=_verified(amount=100)
        ):
            result = fulfilment.fulfil(reference)

        assert result.outcome == fulfilment.OUTCOME_MISMATCH
        assert accounts.get_by_id(account.id).balance_credits == 40

    def test_unknown_reference_grants_nothing(self, billing_env) -> None:
        from dermascan.billing import fulfilment

        result = fulfilment.fulfil("qutis-deadbeef-notours")
        assert result.outcome == fulfilment.OUTCOME_UNKNOWN_REFERENCE

    def test_unsuccessful_payment_grants_nothing(self, billing_env) -> None:
        from dermascan.billing import accounts, fulfilment

        account = accounts.resolve_for_client("telegram", "tg-pay-4")
        reference = self._intent(account.id)

        with patch(
            "dermascan.billing.paystack_client.verify",
            return_value={"status": "failed", "amount": 10000, "currency": "KES"},
        ):
            result = fulfilment.fulfil(reference)

        assert result.outcome == fulfilment.OUTCOME_NOT_SUCCESSFUL
        assert accounts.get_by_id(account.id).balance_credits == 40

    def test_pending_task_is_returned_for_the_resume_offer(self, billing_env) -> None:
        from dermascan.billing import accounts, fulfilment, redis_wallet

        account = accounts.resolve_for_client("telegram", "tg-pay-5")
        redis_wallet.set_pending_task(account.id, "a skin review")
        reference = self._intent(account.id)

        with patch("dermascan.billing.paystack_client.verify", return_value=_verified()):
            result = fulfilment.fulfil(reference)

        assert result.pending_task == "a skin review"
        assert redis_wallet.take_pending_task(account.id) is None


class TestWalletInvariants:
    def test_spend_settles_once_and_reconciles(self, billing_env) -> None:
        from dermascan.billing import accounts, ledger, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-spend-1")
        wallet.hydrate(account.id)

        for checkpoint, credits in (("llm_call", 6), ("tool_call", 9), ("medgemma", 15)):
            assert wallet.deduct(
                account.id, credits=credits, checkpoint=checkpoint, request_id="req-1"
            ).ok

        assert wallet.get_balance(account.id) == 10
        assert wallet.wallet_state(account.id)["unsettled"] == 30

        assert wallet.settle(account.id, "req-1", job_type="skin_audit") == 30
        assert wallet.settle(account.id, "req-1") == 0  # replay is a no-op

        cached, summed = ledger.verify_balance(account.id)
        assert cached == summed == 10
        assert wallet.wallet_state(account.id)["unsettled"] == 0

    def test_cold_key_is_not_a_zero_balance(self, billing_env) -> None:
        from dermascan.billing import accounts, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-cold-1")
        result = wallet.deduct(
            account.id, credits=5, checkpoint="llm_call", request_id="req-cold"
        )

        assert result.is_cold
        assert not result.ok

    def test_a_second_trip_cannot_spend_what_the_first_took(self, billing_env) -> None:
        from dermascan.billing import accounts, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-race-1")
        wallet.hydrate(account.id)

        assert wallet.deduct(
            account.id, credits=35, checkpoint="medgemma", request_id="trip-a"
        ).ok
        refused = wallet.deduct(
            account.id, credits=35, checkpoint="medgemma", request_id="trip-b"
        )

        assert refused.code == wallet.INSUFFICIENT

    def test_overdraft_floor_lets_an_in_flight_trip_finish(self, billing_env) -> None:
        from dermascan.billing import accounts, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-overdraft-1")
        wallet.hydrate(account.id)

        result = wallet.deduct(
            account.id,
            credits=60,
            checkpoint="medgemma",
            request_id="trip-tail",
            overdraft_floor=-60,
        )
        assert result.ok
        assert result.balance == -20

    def test_topup_commutes_with_an_in_flight_deduction(self, billing_env) -> None:
        """A top-up landing mid-trip must not clobber concurrent spend."""
        from dermascan.billing import accounts, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-commute-1")
        wallet.hydrate(account.id)

        wallet.deduct(account.id, credits=15, checkpoint="llm_call", request_id="trip-c")
        wallet.apply_topup(account.id, credits=100, reference="ref-1")
        wallet.deduct(account.id, credits=5, checkpoint="tool_call", request_id="trip-c")

        assert wallet.get_balance(account.id) == 120

    def test_topup_applied_twice_credits_once(self, billing_env) -> None:
        from dermascan.billing import accounts, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-commute-2")
        wallet.hydrate(account.id)

        wallet.apply_topup(account.id, credits=100, reference="ref-dup")
        wallet.apply_topup(account.id, credits=100, reference="ref-dup")

        assert wallet.get_balance(account.id) == 140

    def test_reaper_settles_a_trip_whose_worker_died(self, billing_env) -> None:
        from dermascan.billing import accounts, ledger, redis_wallet as wallet

        account = accounts.resolve_for_client("telegram", "tg-reap-1")
        wallet.hydrate(account.id)
        wallet.deduct(account.id, credits=12, checkpoint="llm_call", request_id="orphan")

        assert wallet.reap_stale_trips(max_age_s=0) == 1

        cached, summed = ledger.verify_balance(account.id)
        assert cached == summed == 28
        assert wallet.wallet_state(account.id)["unsettled"] == 0


class TestWebhookEndpoint:
    def _post(self, client, payload: dict[str, Any], *, secret: str = SECRET):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
        return client.post(
            "/v1/webhooks/paystack",
            content=body,
            headers={
                "x-paystack-signature": signature,
                "content-type": "application/json",
            },
        )

    @pytest.fixture
    def client(self, billing_env):
        from fastapi.testclient import TestClient

        from dermascan.backend.api import endpoints
        from dermascan.backend.app.main import app

        endpoints._redis = billing_env
        return TestClient(app)

    def test_valid_webhook_is_queued_once(self, client, billing_env) -> None:
        payload = {
            "event": "charge.success",
            "data": {"reference": "qutis-abc-1", "amount": 10000},
        }

        first = self._post(client, payload)
        second = self._post(client, payload)

        assert first.status_code == 200
        assert first.json()["status"] == "queued"
        assert second.json()["status"] == "duplicate"
        assert billing_env.xlen("billing:jobs") == 1

    def test_bad_signature_is_rejected(self, client, billing_env) -> None:
        response = self._post(
            client,
            {"event": "charge.success", "data": {"reference": "r", "amount": 1}},
            secret="sk_test_wrong",
        )

        assert response.status_code == 401
        assert billing_env.xlen("billing:jobs") == 0

    def test_endpoint_is_hidden_when_billing_is_disabled(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BILLING_ENABLED", "false")
        response = self._post(
            client, {"event": "charge.success", "data": {"reference": "r", "amount": 1}}
        )
        assert response.status_code == 404


class TestWorkerProcessing:
    def test_worker_credits_the_account_and_messages_the_user(
        self, billing_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dermascan.billing import accounts, intents, paystack_client
        from dermascan.backend.services.agents import billing_worker

        monkeypatch.setattr(billing_worker, "_redis", billing_env, raising=True)

        account = accounts.resolve_for_client("telegram", "tg-worker-1")
        reference = paystack_client.new_reference(account.id)
        intents.create(
            account_id=account.id,
            reference=reference,
            amount_minor=10000,
            currency="KES",
            credits_to_grant=100,
            method=intents.METHOD_CHECKOUT,
        )

        with patch("dermascan.billing.paystack_client.verify", return_value=_verified()):
            billing_worker._process_message(
                "1-0", {"reference": reference, "event": "charge.success", "dedupe_key": "d1"}
            )

        assert accounts.get_by_id(account.id).balance_credits == 140

        messages = billing_env.xrange("outbound:messages")
        assert len(messages) == 1
        text = messages[0][1]["text"]
        assert "confirmed" in text.lower() and "140" in text


class TestChatTools:
    """The tools the model calls. Both return statuses, never exceptions."""

    def _tools(self):
        from dermascan.backend.services.tools.orchestrator import billing_tools

        return billing_tools

    def test_balance_tool_reports_credits(self, billing_env) -> None:
        from dermascan.billing import accounts

        accounts.remember_channel("tg-tool-1", "telegram")
        accounts.resolve_for_client("telegram", "tg-tool-1")

        result = self._tools().get_credit_balance.invoke({"client_id": "tg-tool-1"})

        assert result["status"] == "ok"
        assert result["balance_credits"] == 40
        assert result["prices"]["skin_audit"] == 20

    def test_topup_without_a_number_returns_a_link(self, billing_env) -> None:
        from dermascan.billing import accounts

        accounts.remember_channel("tg-tool-2", "telegram")
        accounts.resolve_for_client("telegram", "tg-tool-2")

        charge = type(
            "R", (), {"authorization_url": "https://checkout.paystack.com/xyz", "raw": {}}
        )()
        with patch(
            "dermascan.billing.paystack_client.initialize_transaction", return_value=charge
        ):
            result = self._tools().create_topup.invoke(
                {"client_id": "tg-tool-2", "amount_credits": 100}
            )

        assert result["status"] == "checkout_link"
        assert result["authorization_url"] == "https://checkout.paystack.com/xyz"

    def test_topup_with_a_volunteered_number_sends_stk(self, billing_env) -> None:
        from dermascan.billing import accounts

        accounts.remember_channel("tg-tool-3", "telegram")
        accounts.resolve_for_client("telegram", "tg-tool-3")

        charge = type("R", (), {"status": "pay_offline", "raw": {}, "display_text": None})()
        with patch(
            "dermascan.billing.paystack_client.charge_mobile_money", return_value=charge
        ):
            result = self._tools().create_topup.invoke(
                {
                    "client_id": "tg-tool-3",
                    "amount_credits": 100,
                    "msisdn": "+254722000000",
                }
            )

        assert result["status"] == "stk_sent"
        assert "+254722000000" in result["instruction"]

    def test_failed_stk_falls_back_to_a_link(self, billing_env) -> None:
        """A user must never be left with no way to pay."""
        from dermascan.billing import accounts

        accounts.remember_channel("tg-tool-4", "telegram")
        accounts.resolve_for_client("telegram", "tg-tool-4")

        charge = type(
            "R", (), {"authorization_url": "https://checkout.paystack.com/fb", "raw": {}}
        )()
        with patch(
            "dermascan.billing.paystack_client.charge_mobile_money",
            side_effect=RuntimeError("paystack said no"),
        ), patch(
            "dermascan.billing.paystack_client.initialize_transaction", return_value=charge
        ):
            result = self._tools().create_topup.invoke(
                {
                    "client_id": "tg-tool-4",
                    "amount_credits": 100,
                    "msisdn": "+254722000000",
                }
            )

        assert result["status"] == "checkout_link"
        assert "https://checkout.paystack.com/fb" in result["instruction"]

    def test_below_minimum_is_refused_with_guidance(self, billing_env) -> None:
        from dermascan.billing import accounts

        accounts.remember_channel("tg-tool-5", "telegram")
        accounts.resolve_for_client("telegram", "tg-tool-5")

        result = self._tools().create_topup.invoke(
            {"client_id": "tg-tool-5", "amount_credits": 10}
        )

        assert result["status"] == "invalid_amount"
        assert result["minimum_credits"] == 100

    def test_tools_are_inert_when_billing_is_disabled(
        self, billing_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BILLING_ENABLED", "false")
        result = self._tools().get_credit_balance.invoke({"client_id": "anyone"})
        assert result["status"] == "billing_disabled"


class TestChannelResolution:
    """Accounts are keyed on (channel, client_id); tools only get client_id."""

    def test_memo_resolves_the_channel(self, billing_env) -> None:
        from dermascan.billing import accounts

        accounts.remember_channel("tg-chan-1", "telegram")
        assert accounts.resolve_channel("tg-chan-1") == "telegram"

    def test_falls_back_to_the_account_table_when_the_memo_expires(
        self, billing_env
    ) -> None:
        from dermascan.billing import accounts

        accounts.resolve_for_client("whatsapp", "+254700111222")
        billing_env.delete("billing:channel:+254700111222")

        assert accounts.resolve_channel("+254700111222") == "whatsapp"

    def test_ambiguous_client_on_two_channels_is_not_guessed(self, billing_env) -> None:
        from dermascan.billing import accounts

        accounts.resolve_for_client("whatsapp", "dual-id")
        accounts.resolve_for_client("telegram", "dual-id")
        billing_env.delete("billing:channel:dual-id")

        assert accounts.resolve_channel("dual-id") is None


class TestAdminApi:
    """HMAC-signed, read-only, and never clinical content."""

    secret = "test-internal-secret"

    @pytest.fixture
    def admin_client(self, billing_env, monkeypatch: pytest.MonkeyPatch):
        from fastapi.testclient import TestClient

        from dermascan.backend.api import endpoints
        from dermascan.backend.app.main import app

        monkeypatch.setenv("QUTIS_INTERNAL_SECRET", self.secret)
        endpoints._redis = billing_env
        return TestClient(app)

    def _get(self, client, path: str, *, secret: str | None = None):
        import time

        from dermascan.shared import hmac_auth

        ts = str(int(time.time()))
        key = secret or self.secret
        signature = hmac.new(
            key.encode(), f"{ts}.".encode() + path.encode(), hashlib.sha256
        ).hexdigest()
        return client.get(
            path,
            headers={
                hmac_auth.SIGNATURE_HEADER: f"sha256={signature}",
                hmac_auth.TIMESTAMP_HEADER: ts,
            },
        )

    def test_overview_returns_totals(self, admin_client, billing_env) -> None:
        from dermascan.billing import accounts, fulfilment, intents, paystack_client

        account = accounts.resolve_for_client("telegram", "tg-admin-1")
        reference = paystack_client.new_reference(account.id)
        intents.create(
            account_id=account.id, reference=reference, amount_minor=10000,
            currency="KES", credits_to_grant=100, method=intents.METHOD_CHECKOUT,
        )
        with patch("dermascan.billing.paystack_client.verify", return_value=_verified()):
            fulfilment.fulfil(reference)

        response = self._get(admin_client, "/v1/admin/billing/overview?days=30")

        assert response.status_code == 200
        body = response.json()
        assert body["accounts"] == 1
        assert body["credits_sold"] == 100
        assert body["revenue_kes"] == 100
        assert body["outstanding_credits"] == 140

    def test_unsigned_request_is_rejected(self, admin_client) -> None:
        assert admin_client.get("/v1/admin/billing/overview").status_code == 401

    def test_wrong_secret_is_rejected(self, admin_client) -> None:
        response = self._get(
            admin_client, "/v1/admin/billing/overview", secret="not-the-secret"
        )
        assert response.status_code == 401

    def test_signature_is_bound_to_the_query_string(self, admin_client) -> None:
        """A signature captured for one page must not work on another."""
        import time

        from dermascan.shared import hmac_auth

        ts = str(int(time.time()))
        signed_path = "/v1/admin/billing/users?limit=1"
        signature = hmac.new(
            self.secret.encode(), f"{ts}.".encode() + signed_path.encode(), hashlib.sha256
        ).hexdigest()

        replayed = admin_client.get(
            "/v1/admin/billing/users?limit=200",
            headers={
                hmac_auth.SIGNATURE_HEADER: f"sha256={signature}",
                hmac_auth.TIMESTAMP_HEADER: ts,
            },
        )
        assert replayed.status_code == 401

    def test_whatsapp_numbers_are_masked(self, admin_client) -> None:
        from dermascan.billing import accounts

        accounts.resolve_for_client("whatsapp", "+254722123456")

        response = self._get(admin_client, "/v1/admin/billing/users?limit=50")
        item = response.json()["items"][0]

        assert "client_id" not in item
        assert item["client_id_masked"].startswith("+2547")
        assert item["client_id_masked"].endswith("456")
        assert "722123" not in item["client_id_masked"]

    def test_unknown_account_is_404(self, admin_client) -> None:
        response = self._get(
            admin_client, "/v1/admin/billing/users/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    def test_reconciliation_reports_healthy(self, admin_client) -> None:
        from dermascan.billing import accounts

        accounts.resolve_for_client("telegram", "tg-admin-2")
        response = self._get(admin_client, "/v1/admin/billing/reconciliation")

        assert response.json() == {"drift": [], "healthy": True}

    def test_admin_api_is_hidden_when_billing_is_disabled(
        self, admin_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BILLING_ENABLED", "false")
        assert self._get(admin_client, "/v1/admin/billing/overview").status_code == 404
