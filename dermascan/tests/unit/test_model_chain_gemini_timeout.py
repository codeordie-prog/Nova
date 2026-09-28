"""Unit tests for Gemini HTTP timeout on chat model construction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dermascan.backend.services.agents.build import build_gemini_chat_model


@pytest.mark.unit
class TestBuildGeminiChatModelTimeout:
    """Gemini clients receive timeout and controlled SDK retries."""

    def test_passes_timeout_and_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_gemini_chat_model maps request_timeout_s to LangChain timeout."""
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
        monkeypatch.setenv("GEMINI_API_KEY_QUTIS", "test-key")

        with patch(
            "dermascan.backend.services.agents.build.ChatGoogleGenerativeAI",
            return_value=MagicMock(),
        ) as chat_cls:
            build_gemini_chat_model(
                model="gemini-3.5-flash",
                temperature=0.0,
                max_tokens=1024,
                google_credentials_path="",
                google_project="proj",
                google_location="global",
                gemini_api_key="test-key",
                request_timeout_s=75.0,
                max_retries=1,
            )

        chat_cls.assert_called_once()
        kwargs = chat_cls.call_args.kwargs
        assert kwargs["timeout"] == 75.0
        assert kwargs["max_retries"] == 1
