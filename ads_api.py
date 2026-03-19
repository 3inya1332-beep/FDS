from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from config import ADS_TIMEOUT_SECONDS, API_KEY, LOCAL_API_BASE


class AdsApiError(RuntimeError):
    pass


@dataclass
class AdsBrowserSession:
    profile_id: str
    cdp_url: str


class AdsApiClient:
    def __init__(
        self,
        base_url: str = LOCAL_API_BASE,
        api_key: str = API_KEY,
        timeout_seconds: int = ADS_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key and self.api_key != "PASTE_YOUR_ADS_API_KEY":
            # Different ADS API builds may use one of these headers.
            headers["Authorization"] = self.api_key
            headers["X-API-KEY"] = self.api_key
            headers["api-key"] = self.api_key
        return headers

    def _auth_params(self) -> dict[str, str]:
        if not self.api_key or self.api_key == "PASTE_YOUR_ADS_API_KEY":
            return {}
        # Different ADS builds expect different query names.
        return {
            "api-key": self.api_key,
            "api_key": self.api_key,
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        params = kwargs.pop("params", None) or {}
        params.update(self._auth_params())
        kwargs["params"] = params

        retries = 4
        for attempt in range(retries):
            response = requests.request(
                method=method.upper(),
                url=url,
                timeout=self.timeout_seconds,
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise AdsApiError(f"Unexpected ADS API response: {payload!r}")

            msg = str(payload.get("msg", "")).lower()
            if "too many request per second" in msg and attempt < retries - 1:
                # ADS local API has strict QPS limits.
                backoff = 0.7 * (2**attempt)
                import time

                time.sleep(backoff)
                continue
            return payload

        raise AdsApiError("ADS API request failed after retries")

    @staticmethod
    def _extract_cdp_url(data: dict[str, Any]) -> str | None:
        debug_port = data.get("debug_port") or data.get("debugPort")
        if debug_port:
            return f"http://127.0.0.1:{debug_port}"

        ws_data = data.get("ws")
        if isinstance(ws_data, dict):
            puppeteer_ws = ws_data.get("puppeteer") or ws_data.get("cdp")
            if isinstance(puppeteer_ws, str) and puppeteer_ws.startswith("ws"):
                parsed = urlparse(puppeteer_ws)
                if parsed.hostname and parsed.port:
                    return f"http://{parsed.hostname}:{parsed.port}"

        direct_ws = data.get("wsEndpoint") or data.get("webSocketDebuggerUrl")
        if isinstance(direct_ws, str) and direct_ws.startswith("ws"):
            parsed = urlparse(direct_ws)
            if parsed.hostname and parsed.port:
                return f"http://{parsed.hostname}:{parsed.port}"

        cdp_url = data.get("cdp_url")
        if isinstance(cdp_url, str) and cdp_url.startswith("http"):
            return cdp_url

        return None

    @staticmethod
    def _data_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
        return payload

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        if "code" in payload:
            return str(payload.get("code")) in {"0", "200"} or payload.get("code") is None
        if "success" in payload:
            return bool(payload["success"])
        return True

    def start_browser(
        self,
        profile_id: str,
        headless: bool = False,
        open_tabs: int = 1,
    ) -> AdsBrowserSession:
        profile_id = profile_id.strip()
        if not self.api_key or self.api_key == "PASTE_YOUR_ADS_API_KEY":
            raise AdsApiError(
                "ADS API requires api-key. Set API_KEY in config.py "
                "(or environment-specific value from your ADS Browser local API)."
            )

        payload_variants = [
            (
                "GET",
                "/api/v1/browser/start",
                {"params": {"user_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "GET",
                "/api/v1/browser/start",
                {"params": {"profile_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "POST",
                "/api/v1/browser/start",
                {"json": {"user_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "POST",
                "/api/v1/browser/start",
                {"json": {"profile_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
        ]

        errors: list[str] = []
        for method, path, kwargs in payload_variants:
            try:
                response_payload = self._request(method, path, **kwargs)
                message = str(response_payload.get("msg", "")).lower()
                if "require api-key" in message:
                    raise AdsApiError(
                        "ADS API returned 'Require api-key'. "
                        "Set valid API_KEY in config.py and ensure local API accepts it."
                    )
                if not self._is_success(response_payload):
                    errors.append(str(response_payload))
                    # Avoid hitting ADS rate limits while trying fallback payloads.
                    import time

                    time.sleep(0.7)
                    continue
                data = self._data_from_payload(response_payload)
                cdp_url = self._extract_cdp_url(data)
                if cdp_url:
                    return AdsBrowserSession(profile_id=profile_id, cdp_url=cdp_url)
                errors.append(f"CDP endpoint not found in payload: {response_payload}")
                import time

                time.sleep(0.7)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {path} failed: {exc}")
                import time

                time.sleep(0.7)

        raise AdsApiError("Unable to start ADS browser profile. " + " | ".join(errors))

    def stop_browser(self, profile_id: str) -> None:
        profile_id = profile_id.strip()
        stop_variants = [
            ("GET", "/api/v1/browser/stop", {"params": {"user_id": profile_id}}),
            ("GET", "/api/v1/browser/stop", {"params": {"profile_id": profile_id}}),
            ("POST", "/api/v1/browser/stop", {"json": {"user_id": profile_id}}),
            ("POST", "/api/v1/browser/stop", {"json": {"profile_id": profile_id}}),
        ]
        for method, path, kwargs in stop_variants:
            try:
                self._request(method, path, **kwargs)
                return
            except Exception:  # noqa: BLE001
                continue
