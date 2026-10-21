"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.

Unit tests for MedGemma worker stream heartbeats during long inference."""


from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

if "httpx" not in sys.modules:
    sys.modules["httpx"] = ModuleType("httpx")

_modal_stub = ModuleType("modal")
_modal_stub.App = MagicMock
_modal_stub.Image = MagicMock
_modal_stub.Secret = MagicMock
_modal_stub.Method = MagicMock
_modal_stub.enter = lambda *a, **k: (lambda f: f)
_modal_stub.exit = lambda *a, **k: None
sys.modules.setdefault("modal", _modal_stub)

_worker_mod = None
_import_error: Exception | None = None

try:
    _fake_redis = MagicMock()
    with patch(
        "dermascan.ai_service.modal.medgemma_service.redis_client.redis_client_from_env",
        return_value=MagicMock(get_redis=lambda: _fake_redis),
    ):
        from dermascan.ai_service.modal.medgemma_service import worker as _worker_mod
except Exception as exc:  # pragma: no cover - environment-specific import deps
    _import_error = exc

pytestmark = pytest.mark.skipif(
    _worker_mod is None,
    reason=f"medgemma worker import unavailable: {_import_error}",
)


@pytest.mark.unit
class TestMedgemmaWorkerHeartbeat:
    """Heartbeat lifecycle around batch inference."""

    def test_start_msg_heartbeat_delegates_to_shared_helper(self) -> None:
        """MedGemma worker uses the shared stream heartbeat helper."""
        stop = threading.Event()
        with patch.object(_worker_mod, "start_stream_heartbeat", return_value=stop) as start_mock:
            got = _worker_mod._start_msg_heartbeat("3-0")
        assert got is stop
        start_mock.assert_called_once()
        kwargs = start_mock.call_args.kwargs
        assert kwargs["stream"] == _worker_mod._JOB_STREAM
        assert kwargs["group"] == _worker_mod._CONSUMER_GROUP
        assert kwargs["msg_id"] == "3-0"
        assert kwargs["interval_sec"] == _worker_mod._MEDGEMMA_HEARTBEAT_INTERVAL_SEC

    def test_collect_batch_result_stops_heartbeat_after_success(self) -> None:
        """Heartbeats run through inference and stop only after ack path."""
        stop = threading.Event()
        entry = _worker_mod._JobEntry(
            job_id="j1",
            prompt="p",
            image_bytes=b"x",
            msg_id="1-0",
            request_id="r",
            case_id="c",
            client_id="cl",
            message_id="m",
            channel="whatsapp",
            group_id="",
            heartbeat_stop=stop,
        )
        handle = MagicMock()
        handle.get.return_value = ("boot", ["ok"])

        with patch.object(_worker_mod, "run_traced_stream_handler") as traced:
            with patch.object(_worker_mod, "route_case_after_inference"):
                with patch.object(_worker_mod, "_mark_status"):
                    with patch.object(_worker_mod, "_settle_job_billing"):
                        with patch.object(_worker_mod.reclaim_logic, "mark_processed"):
                            traced.side_effect = lambda **kw: kw["handler"]()
                            _worker_mod._collect_batch_result([entry], handle)

        assert stop.is_set()

    def test_collect_batch_result_stops_heartbeat_on_inference_failure(self) -> None:
        """Failed batch inference stops heartbeats before ack."""
        stop = threading.Event()
        entry = _worker_mod._JobEntry(
            job_id="j1",
            prompt="p",
            image_bytes=b"x",
            msg_id="1-0",
            request_id="r",
            case_id="c",
            client_id="cl",
            message_id="m",
            channel="whatsapp",
            group_id="",
            heartbeat_stop=stop,
        )
        handle = MagicMock()
        handle.get.side_effect = RuntimeError("modal cold start failed")

        with patch.object(_worker_mod, "_mark_status"):
            with patch.object(_worker_mod, "_settle_job_billing"):
                _worker_mod._collect_batch_result([entry], handle)

        assert stop.is_set()
