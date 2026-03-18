from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

import config


class AdsBrowserApiError(RuntimeError):
    pass


@dataclass(slots=True)
class AdsBrowserSession:
    profile_id: str
    selenium_endpoint: str | None
    puppeteer_endpoint: str | None
    debug_port: str | None
    webdriver_path: str | None
    raw: dict[str, Any]


class AdsBrowserClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or config.ADS_API_BASE).rstrip("/")
        self.api_key = api_key if api_key is not None else config.ADS_API_KEY
        self.timeout = timeout or config.ADS_API_TIMEOUT

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "PUT_YOUR_ADS_API_KEY_HERE":
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def start_profile(self, profile_id: str) -> AdsBrowserSession:
        payload = {
            "profile_id": profile_id,
            "last_opened_tabs": "0",
            "proxy_detection": "0",
            "password_filling": "0",
            "password_saving": "0",
            "cdp_mask": "1",
            "headless": str(config.ADS_HEADLESS),
            "launch_args": list(config.ADS_MINIMIZED_LAUNCH_ARGS),
        }

        response = requests.post(
            f"{self.base_url}/api/v2/browser-profile/start",
            json=payload,
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            raise AdsBrowserApiError(
                f"ADS Browser API returned code={data.get('code')} msg={data.get('msg')!r}"
            )

        payload_data = data.get("data") or {}
        ws_data = payload_data.get("ws") or {}
        return AdsBrowserSession(
            profile_id=profile_id,
            selenium_endpoint=ws_data.get("selenium"),
            puppeteer_endpoint=ws_data.get("puppeteer"),
            debug_port=str(payload_data.get("debug_port") or "") or None,
            webdriver_path=payload_data.get("webdriver"),
            raw=data,
        )
