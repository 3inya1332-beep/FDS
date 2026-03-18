from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from config import (
    ADS_FALLBACK_BASE_URLS,
    ADS_MAX_RETRIES_PER_URL,
    ADS_REQUEST_PAUSE_SECONDS,
    ADS_TIMEOUT_SECONDS,
    API_KEY,
    LOCAL_API_BASE,
)


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

    def _base_urls(self) -> list[str]:
        urls = [self.base_url, *ADS_FALLBACK_BASE_URLS]
        deduped: list[str] = []
        for url in urls:
            normalized = url.rstrip("/")
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        return deduped

    def _api_key_ready(self) -> bool:
        return bool(self.api_key and self.api_key != "PASTE_YOUR_ADS_API_KEY")

    def _bearer_value(self) -> str | None:
        if not self._api_key_ready():
            return None
        if self.api_key.lower().startswith("bearer "):
            return self.api_key
        return f"Bearer {self.api_key}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        bearer = self._bearer_value()
        if bearer:
            # AdsPower Local API expects Authorization: Bearer <api_key>.
            headers["Authorization"] = bearer
            # Compatibility fallbacks for alternate ADS builds.
            headers["X-API-KEY"] = self.api_key
            headers["api-key"] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        include_query_key: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        final_params = dict(params or {})
        # Some ADS API builds expect api-key in query params.
        if include_query_key and self._api_key_ready():
            final_params.setdefault("api_key", self.api_key)
            final_params.setdefault("api-key", self.api_key)

        errors: list[str] = []
        for base_url in self._base_urls():
            for attempt in range(1, ADS_MAX_RETRIES_PER_URL + 1):
                url = f"{base_url}{path}"
                try:
                    response = requests.request(
                        method=method.upper(),
                        url=url,
                        timeout=self.timeout_seconds,
                        headers=self._headers(),
                        params=final_params,
                        **kwargs,
                    )
                except requests.RequestException as exc:
                    errors.append(f"{method} {url} network error: {exc}")
                    if attempt < ADS_MAX_RETRIES_PER_URL:
                        time.sleep(ADS_REQUEST_PAUSE_SECONDS * attempt)
                    continue

                if response.status_code == 503:
                    errors.append(f"{method} {url} -> 503 Service Unavailable")
                    if attempt < ADS_MAX_RETRIES_PER_URL:
                        time.sleep(ADS_REQUEST_PAUSE_SECONDS * attempt)
                    continue

                if response.status_code >= 500:
                    errors.append(f"{method} {url} -> HTTP {response.status_code}")
                    if attempt < ADS_MAX_RETRIES_PER_URL:
                        time.sleep(ADS_REQUEST_PAUSE_SECONDS * attempt)
                    continue

                if response.status_code >= 400:
                    raise AdsApiError(
                        f"ADS API HTTP {response.status_code} for {method} {url}: {response.text[:500]}"
                    )

                try:
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001
                    raise AdsApiError(f"ADS API returned non-JSON response for {method} {url}: {exc}") from exc

                if isinstance(payload, dict):
                    return payload
                raise AdsApiError(f"Unexpected ADS API response: {payload!r}")

        raise AdsApiError(
            "ADS Local API недоступен (503/connection). Проверьте, что AdsPower запущен, "
            "Local API включен, и порт 50325 доступен. "
            f"Детали: {' | '.join(errors)}"
        )

    @staticmethod
    def _msg(payload: dict[str, Any]) -> str:
        msg = payload.get("msg")
        if isinstance(msg, str):
            return msg
        return str(payload)

    def _request_success_or_raise(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response_payload = self._request(method, path, include_query_key=False, **kwargs)
        msg = self._msg(response_payload)
        lowered = msg.lower()
        if "require api-key" in lowered or "require api key" in lowered:
            # Some builds require api-key in query, retry once in compatibility mode.
            response_payload = self._request(method, path, include_query_key=True, **kwargs)

        if self._is_success(response_payload):
            return response_payload

        msg = self._msg(response_payload)
        lowered = msg.lower()
        if "require api-key" in lowered or "require api key" in lowered:
            raise AdsApiError(
                "ADS API требует ключ в формате Bearer. Проверьте config.py -> API_KEY "
                "и что ключ включен в AdsPower (Automation -> API)."
            )
        if "too many request" in lowered:
            raise AdsApiError(
                "ADS API вернул лимит запросов (Too many request per second). "
                "Повторите запуск через пару секунд."
            )
        raise AdsApiError(f"ADS API error: {response_payload}")

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
        # Start with the canonical AdsPower v1 GET call.
        payload_variants = [
            (
                "GET",
                "/api/v1/browser/start",
                {"params": {"user_id": profile_id}},
            ),
            (
                "GET",
                "/api/v1/browser/start",
                {"params": {"user_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "POST",
                "/api/v1/browser/start",
                {"json": {"user_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "GET",
                "/api/v1/browser/start",
                {"params": {"profile_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "POST",
                "/api/v1/browser/start",
                {"json": {"profile_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "GET",
                "/api/v1/browser/start",
                {"params": {"serial_number": profile_id}},
            ),
            (
                "POST",
                "/api/v1/browser/start",
                {"json": {"serial_number": profile_id}},
            ),
        ]

        errors: list[str] = []
        for index, (method, path, kwargs) in enumerate(payload_variants):
            try:
                response_payload = self._request_success_or_raise(method, path, **kwargs)
                data = self._data_from_payload(response_payload)
                cdp_url = self._extract_cdp_url(data)
                if cdp_url:
                    return AdsBrowserSession(profile_id=profile_id, cdp_url=cdp_url)
                errors.append(f"CDP endpoint not found in payload: {response_payload}")
            except AdsApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {path} failed: {exc}")
            if index < len(payload_variants) - 1:
                time.sleep(ADS_REQUEST_PAUSE_SECONDS)

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
                self._request(method, path, include_query_key=False, **kwargs)
                return
            except Exception:  # noqa: BLE001
                continue
