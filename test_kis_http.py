"""Tests for kis_http latency wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import kis_http


def test_kis_request_records_latency(monkeypatch) -> None:
    monkeypatch.setattr(kis_http, "KIS_SLOW_API_MS", 10_000)

    response = MagicMock()
    response.status_code = 200

    with patch("kis_http.requests.request", return_value=response) as mock_request:
        result = kis_http.kis_request("GET", "https://example.test/path", label="unit")

    assert result is response
    assert mock_request.called
    assert kis_http.last_api_response_ms() is not None
    assert kis_http.last_api_response_ms() >= 0


def test_is_retryable_request_error() -> None:
    import requests

    assert kis_http.is_retryable_request_error(requests.Timeout("slow"))
    assert kis_http.is_retryable_request_error(requests.ConnectionError("down"))
    assert not kis_http.is_retryable_request_error(RuntimeError("business rule"))


if __name__ == "__main__":
    class _MonkeyPatch:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    test_kis_request_records_latency(_MonkeyPatch())
    test_is_retryable_request_error()
    print("test_kis_http.py - ALL PASS")
