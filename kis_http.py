"""KIS HTTP transport with per-request latency telemetry."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

KIS_SLOW_API_MS = int(os.getenv("KIS_SLOW_API_MS", "3000"))
_last_api_response_ms: float | None = None


def last_api_response_ms() -> float | None:
    """Return latency (ms) of the most recent KIS HTTP call, if any."""
    return _last_api_response_ms


def kis_request(
    method: str,
    url: str,
    *,
    label: str = "",
    **kwargs: Any,
) -> requests.Response:
    """Issue an HTTP request and emit ``api_response_time`` telemetry."""
    global _last_api_response_ms

    start = time.perf_counter()
    response = requests.request(method, url, **kwargs)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _last_api_response_ms = elapsed_ms

    suffix = f" {label}" if label else ""
    print(
        f"[KIS/HTTP]{suffix} {method.upper()} {elapsed_ms:.0f}ms "
        f"status={response.status_code}"
    )
    if elapsed_ms > KIS_SLOW_API_MS:
        print(
            f"[KIS/SLOW]{suffix} api_response_time={elapsed_ms:.0f}ms "
            f"threshold={KIS_SLOW_API_MS}ms"
        )

    return response


def is_retryable_request_error(exc: BaseException) -> bool:
    """True for transient network / server errors worth retrying."""
    if isinstance(exc, requests.Timeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        return True
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is not None and status >= 500
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", 0) >= 500:
        return True
    return False
