from __future__ import annotations

import time
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
        self._preferred_auth_variant: int | None = None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")

        retries = 4
        for attempt in range(retries):
            response = requests.request(
                method=method.upper(),
                url=url,
                timeout=self.timeout_seconds,
                headers=headers,
                **kwargs,
            )
            if response.status_code == 429 and attempt < retries - 1:
                # HTTP-level rate limit.
                backoff = 0.7 * (2**attempt)
                time.sleep(backoff)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise AdsApiError(f"Unexpected ADS API response: {payload!r}")

            msg = str(payload.get("msg", "")).lower()
            if "too many request per second" in msg and attempt < retries - 1:
                # ADS local API has strict QPS limits.
                backoff = 0.7 * (2**attempt)
                time.sleep(backoff)
                continue
            return payload

        raise AdsApiError("ADS API request failed after retries")

    @staticmethod
    def _msg(payload: dict[str, Any]) -> str:
        return str(payload.get("msg", "")).strip()

    @staticmethod
    def _requires_api_key(payload: dict[str, Any]) -> bool:
        return "require api-key" in str(payload.get("msg", "")).lower()

    def _auth_variants(self) -> list[tuple[dict[str, str], dict[str, str]]]:
        if not self.api_key or self.api_key == "PASTE_YOUR_ADS_API_KEY":
            return [({}, {})]
        key = self.api_key
        return [
            ({"Accept": "application/json", "api-key": key}, {}),
            ({"Accept": "application/json", "Api-Key": key}, {}),
            ({"Accept": "application/json", "X-API-KEY": key}, {}),
            ({"Accept": "application/json", "X-Api-Key": key}, {}),
            ({"Accept": "application/json", "Authorization": key}, {}),
            ({"Accept": "application/json", "Authorization": f"Bearer {key}"}, {}),
            ({"Accept": "application/json"}, {"api-key": key}),
            ({"Accept": "application/json"}, {"api_key": key}),
            ({"Accept": "application/json"}, {"apikey": key}),
            (
                {
                    "Accept": "application/json",
                    "api-key": key,
                    "Authorization": f"Bearer {key}",
                },
                {"api-key": key},
            ),
        ]

    def _request_with_auth_fallback(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_params = dict(params or {})
        errors: list[str] = []
        variants = self._auth_variants()
        if self._preferred_auth_variant is not None and 0 <= self._preferred_auth_variant < len(variants):
            preferred = variants[self._preferred_auth_variant]
            variants = [preferred] + [v for idx, v in enumerate(variants) if idx != self._preferred_auth_variant]

        for index, (headers, auth_params) in enumerate(variants, start=1):
            merged_params = dict(base_params)
            merged_params.update(auth_params)
            kwargs: dict[str, Any] = {"params": merged_params, "headers": headers}
            if json_payload is not None:
                kwargs["json"] = json_payload
            try:
                payload = self._request(method, path, **kwargs)
                if self._requires_api_key(payload):
                    errors.append(f"auth_variant#{index}: {self._msg(payload)}")
                    time.sleep(0.1)
                    continue
                # Cache successful auth variant index for faster next calls.
                if self._preferred_auth_variant is None:
                    for original_idx, original in enumerate(self._auth_variants()):
                        if original == (headers, auth_params):
                            self._preferred_auth_variant = original_idx
                            break
                return payload
            except requests.HTTPError as exc:
                errors.append(f"auth_variant#{index}: HTTP {exc}")
                time.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"auth_variant#{index}: {exc}")
                time.sleep(0.1)

        raise AdsApiError("Auth fallback exhausted. " + " | ".join(errors))

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
        ]

        errors: list[str] = []
        for method, path, kwargs in payload_variants:
            try:
                response_payload = self._request_with_auth_fallback(
                    method=method,
                    path=path,
                    params=kwargs.get("params"),
                    json_payload=kwargs.get("json"),
                )
                if not self._is_success(response_payload):
                    errors.append(str(response_payload))
                    # Avoid hitting ADS rate limits while trying fallback payloads.
                    time.sleep(0.2)
                    continue
                data = self._data_from_payload(response_payload)
                cdp_url = self._extract_cdp_url(data)
                if cdp_url:
                    return AdsBrowserSession(profile_id=profile_id, cdp_url=cdp_url)
                errors.append(f"CDP endpoint not found in payload: {response_payload}")
                time.sleep(0.2)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {path} failed: {exc}")
                time.sleep(0.2)

        raise AdsApiError("Unable to start ADS browser profile. " + " | ".join(errors))

    def stop_browser(self, profile_id: str) -> None:
        profile_id = profile_id.strip()
        stop_variants = [
            ("GET", "/api/v1/browser/stop", {"params": {"user_id": profile_id}}),
            ("GET", "/api/v1/browser/stop", {"params": {"profile_id": profile_id}}),
        ]
        for method, path, kwargs in stop_variants:
            try:
                self._request_with_auth_fallback(
                    method=method,
                    path=path,
                    params=kwargs.get("params"),
                    json_payload=kwargs.get("json"),
                )
                return
            except Exception:  # noqa: BLE001
                continue
