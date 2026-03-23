from __future__ import annotations

from typing import Any

import requests

from config import (
    NINEPROXY_API_KEY,
    NINEPROXY_PORT,
    NINEPROXY_ROTATE_ENABLED,
    NINEPROXY_ROTATE_METHOD,
    NINEPROXY_ROTATE_URL,
    NINEPROXY_TIMEOUT_SECONDS,
)


class ProxyRotationError(RuntimeError):
    pass


class NineProxyClient:
    """
    Minimal 9proxy rotation client.

    Endpoint format differs between 9proxy plans, so URL/method are kept configurable.
    Rotation is called only on explicit errors (e.g. captcha/registration failure),
    not after each processed batch.
    """

    def __init__(self) -> None:
        self.enabled = NINEPROXY_ROTATE_ENABLED and bool(NINEPROXY_ROTATE_URL.strip())
        self.rotate_url = NINEPROXY_ROTATE_URL.strip()
        self.method = NINEPROXY_ROTATE_METHOD.strip().upper()
        self.timeout = NINEPROXY_TIMEOUT_SECONDS
        self.api_key = NINEPROXY_API_KEY.strip()
        self.port = NINEPROXY_PORT.strip()

    def rotate(self) -> None:
        if not self.enabled:
            return

        params: dict[str, Any] = {}
        if self.api_key:
            params["api-key"] = self.api_key
        if self.port:
            params["port"] = self.port

        response = requests.request(
            method=self.method,
            url=self.rotate_url,
            params=params if self.method == "GET" else None,
            json=params if self.method != "GET" else None,
            timeout=self.timeout,
        )
        response.raise_for_status()
        # Best-effort validation only, because response format can differ.
        body = response.text.strip().lower()
        if body and any(word in body for word in ("error", "failed", "invalid")):
            raise ProxyRotationError(f"9proxy rotation returned error: {response.text}")
