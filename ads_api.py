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

    def _header_variants(self) -> list[dict[str, str]]:
        headers_base = {"Accept": "application/json"}
        if not self.api_key or self.api_key == "PASTE_YOUR_ADS_API_KEY":
            return [headers_base]

        common = {
            "X-AUTHORIZATION": self.api_key,
            "X-API-KEY": self.api_key,
            "api-key": self.api_key,
            "api_key": self.api_key,
            "apikey": self.api_key,
        }
        return [
            {**headers_base, **common, "Authorization": f"Bearer {self.api_key}"},
            {**headers_base, **common, "Authorization": self.api_key},
        ]

    def _request(self, method: str, path: str, *, retry_on_rate_limit: int = 2, **kwargs: Any) -> dict[str, Any]:
        params = dict(kwargs.pop("params", {}) or {})
        if self.api_key and self.api_key != "PASTE_YOUR_ADS_API_KEY":
            # Some ADS API builds accept key only as query param.
            params.setdefault("api-key", self.api_key)
            params.setdefault("api_key", self.api_key)
            params.setdefault("apikey", self.api_key)
            params.setdefault("apiKey", self.api_key)
        kwargs["params"] = params

        json_payload = kwargs.get("json")
        if isinstance(json_payload, dict) and self.api_key and self.api_key != "PASTE_YOUR_ADS_API_KEY":
            # Other ADS builds accept key in body.
            json_payload.setdefault("api-key", self.api_key)
            json_payload.setdefault("api_key", self.api_key)
            json_payload.setdefault("apikey", self.api_key)
            json_payload.setdefault("apiKey", self.api_key)
            kwargs["json"] = json_payload

        url = f"{self.base_url}{path}"
        attempts = max(0, retry_on_rate_limit) + 1
        for headers in self._header_variants():
            for attempt in range(attempts):
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    timeout=self.timeout_seconds,
                    headers=headers,
                    **kwargs,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise AdsApiError(f"Unexpected ADS API response: {payload!r}")

                if self._is_rate_limited(payload) and attempt < attempts - 1:
                    time.sleep(1.2 * (attempt + 1))
                    continue

                if self._requires_api_key(payload):
                    # Retry with another auth header format.
                    break
                return payload

        raise AdsApiError("ADS API request failed after retries.")

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

    @staticmethod
    def _is_rate_limited(payload: dict[str, Any]) -> bool:
        message = str(payload.get("msg", "") or payload.get("message", "")).lower()
        return "too many request" in message or "rate limit" in message

    @staticmethod
    def _requires_api_key(payload: dict[str, Any]) -> bool:
        message = str(payload.get("msg", "") or payload.get("message", "")).lower()
        return "require api-key" in message or "require api key" in message

    def start_browser(
        self,
        profile_id: str,
        headless: bool = False,
        open_tabs: int = 1,
    ) -> AdsBrowserSession:
        profile_id = profile_id.strip()
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
                "GET",
                "/api/v1/browser/start",
                {"params": {"id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
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
            (
                "POST",
                "/api/v1/browser/start",
                {"json": {"id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "GET",
                "/api/v1/browser/open",
                {"params": {"user_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "GET",
                "/api/v1/browser/open",
                {"params": {"profile_id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
            (
                "GET",
                "/api/v1/browser/open",
                {"params": {"id": profile_id, "headless": int(headless), "open_tabs": open_tabs}},
            ),
        ]

        errors: list[str] = []
        for method, path, kwargs in payload_variants:
            try:
                response_payload = self._request(method, path, **kwargs)
                if not self._is_success(response_payload):
                    errors.append(str(response_payload))
                    time.sleep(0.6)
                    continue
                data = self._data_from_payload(response_payload)
                cdp_url = self._extract_cdp_url(data)
                if cdp_url:
                    return AdsBrowserSession(profile_id=profile_id, cdp_url=cdp_url)
                errors.append(f"CDP endpoint not found in payload: {response_payload}")
                time.sleep(0.6)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {path} failed: {exc}")
                time.sleep(0.6)

        raise AdsApiError("Unable to start ADS browser profile. " + " | ".join(errors))

    def stop_browser(self, profile_id: str) -> None:
        profile_id = profile_id.strip()
        stop_variants = [
            ("GET", "/api/v1/browser/stop", {"params": {"user_id": profile_id}}),
            ("GET", "/api/v1/browser/stop", {"params": {"profile_id": profile_id}}),
            ("GET", "/api/v1/browser/stop", {"params": {"id": profile_id}}),
            ("POST", "/api/v1/browser/stop", {"json": {"user_id": profile_id}}),
            ("POST", "/api/v1/browser/stop", {"json": {"profile_id": profile_id}}),
            ("POST", "/api/v1/browser/stop", {"json": {"id": profile_id}}),
            ("GET", "/api/v1/browser/close", {"params": {"user_id": profile_id}}),
            ("GET", "/api/v1/browser/close", {"params": {"profile_id": profile_id}}),
            ("GET", "/api/v1/browser/close", {"params": {"id": profile_id}}),
        ]
        for method, path, kwargs in stop_variants:
            try:
                self._request(method, path, **kwargs)
                return
            except Exception:  # noqa: BLE001
                continue

    @staticmethod
    def _extract_profile_id(payload: dict[str, Any]) -> str | None:
        data = AdsApiClient._data_from_payload(payload)
        id_keys = ("id", "user_id", "userId", "profile_id", "profileId", "serial_number")
        for key in id_keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

        # Some APIs return created profile in list.
        for key in ("list", "items", "rows", "profiles"):
            value = data.get(key)
            if isinstance(value, list) and value:
                item = value[0]
                if not isinstance(item, dict):
                    continue
                for id_key in id_keys:
                    item_value = item.get(id_key)
                    if item_value is not None and str(item_value).strip():
                        return str(item_value).strip()
        return None

    @staticmethod
    def _apply_proxy_payload(base_payload: dict[str, Any], proxy_config: dict[str, str] | None) -> dict[str, Any]:
        payload = dict(base_payload)
        if not proxy_config:
            return payload

        proxy_soft = proxy_config.get("proxy_soft", "other")
        proxy_type = proxy_config.get("proxy_type", "http")
        proxy_host = proxy_config.get("proxy_host", "")
        proxy_port = proxy_config.get("proxy_port", "")
        proxy_user = proxy_config.get("proxy_user", "") or proxy_config.get("proxy_username", "")
        proxy_pass = proxy_config.get("proxy_password", "")

        if not proxy_host or not proxy_port:
            return payload

        user_proxy_config = {
            "proxy_soft": proxy_soft,
            "proxy_type": proxy_type,
            "proxy_host": proxy_host,
            "proxy_port": str(proxy_port),
            "proxy_user": proxy_user,
            "proxy_password": proxy_pass,
        }

        payload.update(
            {
                "proxy_type": proxy_type,
                "proxy_host": proxy_host,
                "proxy_port": str(proxy_port),
                "proxy_username": proxy_user,
                "proxy_password": proxy_pass,
                "proxy_user": proxy_user,
                "proxy_pass": proxy_pass,
                "user_proxy_config": user_proxy_config,
            }
        )
        return payload

    @staticmethod
    def _desktop_fingerprint_config() -> dict[str, Any]:
        # Force desktop/browser profile to avoid accidental mobile UI mode.
        return {
            "automatic_timezone": "1",
            "webrtc": "proxy",
            "language": ["en-US", "en"],
            "screen_resolution": "1920_1080",
            "ua": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "browser_kernel_config": {"type": "chrome", "version": "ua_auto"},
            "random_ua": {
                "ua_browser": ["chrome"],
                "ua_system_version": ["Windows 10", "Windows 11"],
            },
        }

    def create_profile(self, name: str, proxy_config: dict[str, str] | None = None) -> str:
        profile_name = name.strip()
        if not profile_name:
            raise AdsApiError("ADS profile name cannot be empty.")

        desktop_fp = self._desktop_fingerprint_config()
        minimal_desktop_fp = {
            "automatic_timezone": "1",
            "webrtc": "proxy",
            "language": ["en-US", "en"],
            "screen_resolution": "1920_1080",
            "ua": desktop_fp["ua"],
        }
        payload_name = self._apply_proxy_payload(
            {"name": profile_name, "group_id": "0", "fingerprint_config": desktop_fp},
            proxy_config,
        )
        payload_user_name = self._apply_proxy_payload(
            {"user_name": profile_name, "group_id": "0", "fingerprint_config": desktop_fp},
            proxy_config,
        )
        payload_profile_name = self._apply_proxy_payload(
            {"name": profile_name, "fingerprint_config": desktop_fp},
            proxy_config,
        )
        payload_name_min = self._apply_proxy_payload(
            {"name": profile_name, "group_id": "0", "fingerprint_config": minimal_desktop_fp},
            proxy_config,
        )
        payload_user_name_min = self._apply_proxy_payload(
            {"user_name": profile_name, "group_id": "0", "fingerprint_config": minimal_desktop_fp},
            proxy_config,
        )
        payload_profile_name_min = self._apply_proxy_payload(
            {"name": profile_name, "fingerprint_config": minimal_desktop_fp},
            proxy_config,
        )

        create_variants = [
            ("POST", "/api/v1/user/create", {"json": payload_name}),
            ("POST", "/api/v1/user/create", {"json": payload_user_name}),
            ("POST", "/api/v1/profile/create", {"json": payload_profile_name}),
            ("POST", "/api/v1/profiles/create", {"json": payload_profile_name}),
            ("POST", "/api/v1/user/create", {"json": payload_name_min}),
            ("POST", "/api/v1/user/create", {"json": payload_user_name_min}),
            ("POST", "/api/v1/profile/create", {"json": payload_profile_name_min}),
            ("POST", "/api/v1/profiles/create", {"json": payload_profile_name_min}),
            ("GET", "/api/v1/user/create", {"params": {"name": profile_name, "group_id": "0"}}),
            ("GET", "/api/v1/profile/create", {"params": {"name": profile_name}}),
        ]

        errors: list[str] = []
        for method, path, kwargs in create_variants:
            try:
                payload = self._request(method, path, **kwargs)
                if not self._is_success(payload):
                    errors.append(str(payload))
                    continue
                created_id = self._extract_profile_id(payload)
                if created_id:
                    return created_id
                errors.append(f"Profile id not found in payload: {payload!r}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {path} failed: {exc}")
                continue

        raise AdsApiError("Unable to create ADS profile. " + " | ".join(errors))

    def delete_profile(self, profile_id: str) -> None:
        profile_id = profile_id.strip()
        if not profile_id:
            raise AdsApiError("ADS profile id is empty.")

        delete_variants = [
            ("POST", "/api/v1/user/delete", {"json": {"user_ids": [profile_id]}}),
            ("POST", "/api/v1/user/delete", {"json": {"profile_ids": [profile_id]}}),
            ("POST", "/api/v1/user/delete", {"json": {"user_id": profile_id}}),
            ("POST", "/api/v1/profile/delete", {"json": {"id": profile_id}}),
            ("POST", "/api/v1/profiles/delete", {"json": {"ids": [profile_id]}}),
            ("GET", "/api/v1/user/delete", {"params": {"user_id": profile_id}}),
            ("GET", "/api/v1/user/delete", {"params": {"profile_id": profile_id}}),
            ("GET", "/api/v1/profile/delete", {"params": {"id": profile_id}}),
        ]
        errors: list[str] = []
        for method, path, kwargs in delete_variants:
            try:
                payload = self._request(method, path, **kwargs)
                if self._is_success(payload):
                    return
                errors.append(str(payload))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{method} {path} failed: {exc}")

        raise AdsApiError("Unable to delete ADS profile. " + " | ".join(errors))
